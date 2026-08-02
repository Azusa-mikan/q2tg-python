from __future__ import annotations

"""两个平台之间的消息转换与发送。"""

import asyncio
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import filetype
import httpx
from telegram import (
    Bot,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    ReplyParameters,
)
from telegram.error import TimedOut
from telegram.ext import ExtBot

from src.audio import normalize_onebot_record
from src.config import config
from src.log import baselog
from src.media import MediaFile, media_cache, media_queue_budget
from src.messages import (
    FailureAction,
    MediaTooLargeError,
    OneBotConnectionError,
    OneBotGroupBanEvent,
    OneBotGroupMemberEvent,
    OneBotMessage,
    OneBotPokeEvent,
    OneBotSendError,
    SendLane,
    SendTarget,
    SendTask,
    TelegramMessage,
)
from src.notice import (
    enqueue_bridge_notice,
    enqueue_onebot_notice,
    enqueue_telegram_notice,
)
from src.processing import ProcessingTask
from src.runtime_stats import track_conversion
from src.sql import sql
from src.sticker import static_sticker_to_png, tgs_sticker_to_gif, video_sticker_to_gif
from src.video import normalize_video_for_onebot

if TYPE_CHECKING:
    from src.qbot import QGateway

PHOTO_LIMIT = 10_000_000
ONEBOT_MEDIA_LIMIT = 20_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
MAX_SEND_ATTEMPTS = 3
TELEGRAM_VIDEO_LIMIT = 20_000_000
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096

# 普通转发和撤回由不同消费者执行。这里记录尚未结束的 OneBot 消息，防止撤回
# 事件任务在普通转发保存映射前查询数据库并错误地当作未命中。
_active_onebot_forwards: set[tuple[int, int]] = set()
_pending_onebot_recalls: set[tuple[int, int]] = set()


def begin_onebot_forward(q_group_id: int, q_message_id: int) -> bool:
    """标记 OneBot 消息已进入普通转发队列；重复在途消息返回 False。"""
    key = (q_group_id, q_message_id)
    if key in _active_onebot_forwards:
        return False
    _active_onebot_forwards.add(key)
    return True


def request_onebot_recall(q_group_id: int, q_message_id: int) -> bool:
    """若消息仍在转发则登记待撤回，并返回 True。"""
    key = (q_group_id, q_message_id)
    if key not in _active_onebot_forwards:
        return False
    _pending_onebot_recalls.add(key)
    return True


def abandon_onebot_forward(q_group_id: int, q_message_id: int) -> None:
    """入口未能入队时清除在途标记。"""
    key = (q_group_id, q_message_id)
    _active_onebot_forwards.discard(key)
    _pending_onebot_recalls.discard(key)


async def onebot_message_text(
    message: list[dict[Any, Any]],
    group_id: int,
    gateway: QGateway | None = None,
    *,
    id_show_enabled: bool = False,
    member_names: dict[int, str | None] | None = None,
) -> str:
    """按原顺序拼接 text 和可见的 at segment。"""
    if member_names is None:
        member_names = {}
    user_ids: list[int] = []
    for segment in message:
        if segment.get("type") != "at":
            continue
        data = segment.get("data")
        user_id = _onebot_at_user_id(data.get("qq")) if isinstance(data, dict) else None
        if user_id is not None and user_id not in member_names and user_id not in user_ids:
            user_ids.append(user_id)
    if user_ids:
        names = await asyncio.gather(
            *(_onebot_member_name(gateway, group_id, user_id) for user_id in user_ids)
        )
        member_names.update(zip(user_ids, names, strict=True))

    parts: list[str] = []
    for segment in message:
        kind = segment.get("type")
        data = segment.get("data")
        if kind == "text":
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str):
                parts.append(text)
            continue
        if kind != "at" or not isinstance(data, dict):
            continue
        qq = data.get("qq")
        if qq == "all":
            parts.append("@全体成员")
            continue
        user_id = _onebot_at_user_id(qq)
        if user_id is None:
            continue
        name = member_names.get(user_id)
        if name is not None:
            mention = f"{name}[{user_id}]" if id_show_enabled else name
        else:
            mention = str(user_id) if id_show_enabled else "Onebot用户"
        parts.append(f"@{mention}")
    return "".join(parts)


def _onebot_at_user_id(value: Any) -> int | None:
    """解析 at.qq 的数字账号；all 和畸形值由调用方分别处理或忽略。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _onebot_member_name(
    gateway: QGateway | None,
    group_id: int,
    user_id: int,
    *,
    no_cache: bool = False,
) -> str | None:
    """查询 at 对象的可见名称，失败时返回 None 交给格式层兜底。"""
    if gateway is None:
        return None
    try:
        if no_cache:
            member = await gateway.get_group_member_info(
                group_id,
                user_id,
                no_cache=True,
            )
        else:
            member = await gateway.get_group_member_info(group_id, user_id)
    except Exception:  # noqa: BLE001
        baselog.warning(
            "OneBot 群成员信息查询失败: group=%s user=%s",
            group_id,
            user_id,
        )
        return None
    card = member.get("card")
    nickname = member.get("nickname")
    if isinstance(card, str) and card.strip():
        return card.strip()
    if isinstance(nickname, str) and nickname.strip():
        return nickname.strip()
    return None


def onebot_message_media(
    message: list[dict[Any, Any]],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """提取带 HTTP(S) 下载地址的媒体，并返回缺少地址的媒体类型。"""
    media = []
    unavailable: list[str] = []
    for segment in message:
        kind = segment.get("type")
        if kind not in {"file", "image", "record", "video"}:
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            unavailable.append(kind)
            continue
        url = data.get("url")
        if not isinstance(url, str) or not _is_http_url(url):
            unavailable.append(kind)
            continue
        filename = _onebot_media_filename(data.get("file"), kind)
        media.append((kind, url, filename))
    return media, unavailable


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _onebot_media_filename(file: Any, kind: str) -> str:
    """把入站 data.file 作为文件名，并移除不适合上传文件名的字符。"""
    if isinstance(file, str):
        filename = file.strip().replace("\x00", "").replace("\r", "").replace("\n", "")
        # file 在入站消息中是文件名，不是本地路径；分隔符只做安全替换。
        filename = filename.replace("/", "_")
        if filename and filename not in {".", ".."}:
            return filename
    if kind == "video":
        return "video.mp4"
    if kind == "record":
        return "voice.silk"
    return "image" if kind == "image" else "file"


async def download_media(
    client: httpx.AsyncClient,
    url: str,
    *,
    filename: str,
    kind: str,
) -> MediaFile:
    """把 OneBot 媒体流式下载到 spool，失败时关闭文件。"""
    fallback_type = {
        "image": "image/jpeg",
        "record": "audio/silk",
        "video": "video/mp4",
    }.get(kind, "application/octet-stream")
    media = await MediaFile.create(filename=filename, media_type=fallback_type)
    try:
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                response_type = response.headers.get("content-type", "").partition(";")[0]
                if response_type:
                    media.media_type = response_type
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > ONEBOT_MEDIA_LIMIT:
                    raise MediaTooLargeError("Onebot 媒体超过 20 MB，无法转发")
                async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_SIZE):
                    if media.size + len(chunk) > ONEBOT_MEDIA_LIMIT:
                        raise MediaTooLargeError("Onebot 媒体超过 20 MB，无法转发")
                    media.write(chunk)
        except httpx.HTTPError:
            raise RuntimeError("OneBot 媒体下载失败") from None
        media.rewind()
        return media
    except BaseException:
        media.close()
        raise


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    filename: str,
) -> MediaFile:
    """保留明确的图片下载入口供调用方和测试使用。"""
    return await download_media(client, url, filename=filename, kind="image")


async def forward_onebot_to_telegram(
    msg: OneBotMessage,
    bot: ExtBot[None],
    client: httpx.AsyncClient,
    gateway: QGateway | None = None,
) -> None:
    """将 OneBot 文本和图片按顺序发送到绑定的 Telegram 群。"""
    if await sql.get_tg_message(msg.group_id, msg.message_id) is not None:
        baselog.warning(
            "忽略已有 Telegram 映射的重复 OneBot 消息: group=%s message=%s",
            msg.group_id,
            msg.message_id,
        )
        return
    group_id = await sql.get_tg_group(msg.group_id)
    if group_id is None:
        baselog.warning("OneBot 群未配置转发目标: %s", msg.group_id)
        return
    msg.tg_chat_id = group_id

    sender_name = msg.sender_name
    has_mentions = any(segment.get("type") == "at" for segment in msg.message)
    id_show_enabled = (
        bool(await sql.get_id_show_enabled(group_id))
        if msg.sender_name_is_fallback or has_mentions
        else False
    )
    text = await onebot_message_text(
        msg.message,
        msg.group_id,
        gateway,
        id_show_enabled=id_show_enabled,
        member_names=msg.mention_names,
    )
    if msg.sender_name_is_fallback and not id_show_enabled:
        sender_name = "OneBot 用户"
    media, unavailable = onebot_message_media(msg.message)
    if not text and not media and not unavailable:
        baselog.warning("OneBot 消息没有可转发的内容: %s", msg.message_id)
        return

    caption = f"{sender_name}:\n{text}" if text else f"{sender_name}:"
    if unavailable:
        labels = {"image": "图片", "video": "视频", "record": "语音", "file": "文件"}
        notices = [
            f"[{labels[kind]}无法转发：缺少可用的 HTTP(S) 下载地址]"
            for kind in dict.fromkeys(unavailable)
        ]
        caption += "\n" + "\n".join(notices)
    reply_parameters = None
    if msg.reply_message_id is not None:
        reply_mapping = await sql.get_tg_message(msg.group_id, msg.reply_message_id)
        if reply_mapping is not None and reply_mapping.tg_message_ids:
            reply_parameters = ReplyParameters(
                message_id=reply_mapping.tg_message_ids[0],
                allow_sending_without_reply=True,
            )

    if not media:
        await _send_text_chunks(msg, bot, group_id, caption, reply_parameters)
    elif (
        msg.next_media_index == 0
        and 2 <= len(media) <= 10
        and all(kind == "image" for kind, _, _ in media)
    ):
        media_caption, media_reply = await _prepare_media_caption(
            msg,
            bot,
            group_id,
            caption,
            reply_parameters,
        )
        contents: list[MediaFile] = []
        try:
            for kind, url, filename in media:
                contents.append(
                    await download_media(
                        client,
                        url,
                        filename=filename,
                        kind=kind,
                    )
                )
            as_animations = any(_is_gif(content) for content in contents)
            as_photos = all(content.size <= PHOTO_LIMIT for content in contents)
            album: list[InputMediaPhoto | InputMediaDocument] = []
            if as_animations:
                await _send_downloaded_media_individually(
                    msg,
                    bot,
                    group_id,
                    media,
                    contents,
                    media_caption,
                    media_reply,
                )
            elif as_photos:
                album = [
                    InputMediaPhoto(
                        media=InputFile(
                            content.file,
                            filename=filename,
                            attach=True,
                            read_file_handle=False,
                        ),
                        caption=media_caption if index == 0 else None,
                        show_caption_above_media=index == 0,
                    )
                    for index, (content, (_, _, filename)) in enumerate(
                        zip(contents, media, strict=True)
                    )
                ]
            else:
                album = [
                    InputMediaDocument(
                        media=InputFile(
                            content.file,
                            filename=filename,
                            attach=True,
                            read_file_handle=False,
                        ),
                        caption=media_caption if index == len(contents) - 1 else None,
                    )
                    for index, (content, (_, _, filename)) in enumerate(
                        zip(contents, media, strict=True)
                    )
                ]
            if not as_animations:
                sent_messages = await bot.send_media_group(
                    chat_id=group_id,
                    media=album,
                    reply_parameters=media_reply,
                )
                msg.tg_message_ids.extend(sent.message_id for sent in sent_messages)
                msg.next_media_index = len(media)
        finally:
            for content in contents:
                content.close()
    else:
        media_caption, media_reply = await _prepare_media_caption(
            msg,
            bot,
            group_id,
            caption,
            reply_parameters,
        )
        for index in range(msg.next_media_index, len(media)):
            kind, url, filename = media[index]
            content = await download_media(
                client,
                url,
                filename=filename,
                kind=kind,
            )
            try:
                if kind == "record":
                    await track_conversion("voice", normalize_onebot_record(content))
                is_gif = kind == "image" and _is_gif(content)
                upload_filename = (
                    f"{Path(filename).stem or 'animation'}.gif" if is_gif else filename
                )
                if kind == "record":
                    upload_filename = content.filename
                upload = InputFile(
                    content.file,
                    filename=upload_filename,
                    read_file_handle=False,
                )
                item_caption = media_caption if not msg.tg_message_ids else None
                item_reply = media_reply if not msg.tg_message_ids else None
                if kind == "record":
                    sent = await bot.send_voice(
                        chat_id=group_id,
                        voice=upload,
                        caption=item_caption,
                        reply_parameters=item_reply,
                    )
                elif is_gif:
                    sent = await bot.send_animation(
                        chat_id=group_id,
                        animation=upload,
                        caption=item_caption,
                        show_caption_above_media=True,
                        reply_parameters=item_reply,
                    )
                elif kind == "image" and content.size <= PHOTO_LIMIT:
                    sent = await bot.send_photo(
                        chat_id=group_id,
                        photo=upload,
                        caption=item_caption,
                        show_caption_above_media=True,
                        reply_parameters=item_reply,
                    )
                elif kind == "video" and _is_mp4(content):
                    sent = await bot.send_video(
                        chat_id=group_id,
                        video=upload,
                        caption=item_caption,
                        show_caption_above_media=True,
                        supports_streaming=True,
                        reply_parameters=item_reply,
                    )
                else:
                    sent = await bot.send_document(
                        chat_id=group_id,
                        document=upload,
                        caption=item_caption,
                        reply_parameters=item_reply,
                    )
                msg.tg_message_ids.append(sent.message_id)
                msg.next_media_index = index + 1
            finally:
                content.close()

    # 远端发送已经完成，本地映射失败不能触发 Telegram 重发。
    try:
        await sql.set_message_mapping(
            q_group_id=msg.group_id,
            q_message_ids=(msg.message_id,),
            tg_chat_id=group_id,
            tg_message_ids=tuple(msg.tg_message_ids),
            q_user_id=msg.user_id,
        )
    except Exception:  # noqa: BLE001
        baselog.exception("Telegram 消息发送成功，但消息映射保存失败")


async def recall_onebot_message_from_telegram(
    q_group_id: int,
    q_message_id: int,
    bot: ExtBot[None],
    *,
    tg_chat_id: int | None = None,
    tg_message_ids: tuple[int, ...] = (),
) -> None:
    """根据 OneBot 消息映射删除 Telegram 侧的全部副本。"""
    if tg_chat_id is None or not tg_message_ids:
        mapping = await sql.get_tg_message(q_group_id, q_message_id)
        if mapping is None:
            baselog.warning(
                "OneBot 撤回事件没有可用的 Telegram 消息映射: group=%s message=%s",
                q_group_id,
                q_message_id,
            )
            return
        tg_chat_id = mapping.tg_chat_id
        tg_message_ids = mapping.tg_message_ids
    for index in range(0, len(tg_message_ids), 100):
        await bot.delete_messages(
            chat_id=tg_chat_id,
            message_ids=tg_message_ids[index : index + 100],
        )


def format_duration(seconds: int) -> str:
    """把非负秒数缩放为天、小时、分钟和秒的中文组合。"""
    units = ((86_400, "天"), (3_600, "小时"), (60, "分钟"), (1, "秒"))
    parts: list[str] = []
    remainder = seconds
    for unit_seconds, label in units:
        value, remainder = divmod(remainder, unit_seconds)
        if value:
            parts.append(f"{value} {label}")
    return " ".join(parts) or "0 秒"


def _event_member_text(name: str | None, user_id: int, *, show_id: bool) -> str:
    if name is None:
        return str(user_id) if show_id else "OneBot用户"
    return f"{name}[{user_id}]" if show_id else name


async def forward_onebot_group_ban_to_telegram(
    event: OneBotGroupBanEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> None:
    """把 OneBot 群禁言事件格式化为 Telegram 文本消息。"""
    tg_chat_id = await sql.get_tg_group(event.group_id)
    if tg_chat_id is None:
        baselog.warning("OneBot 群事件未配置转发目标: %s", event.group_id)
        return
    show_id = bool(await sql.get_id_show_enabled(tg_chat_id))
    user_name, operator_name = await asyncio.gather(
        _onebot_member_name(gateway, event.group_id, event.user_id),
        _onebot_member_name(gateway, event.group_id, event.operator_id),
    )
    user = _event_member_text(user_name, event.user_id, show_id=show_id)
    operator = _event_member_text(
        operator_name,
        event.operator_id,
        show_id=show_id,
    )
    if event.lifted:
        text = f"{user} 被管理员 {operator} 解除禁言"
    else:
        text = f"{user} 被管理员 {operator} 禁言 {format_duration(event.duration)}"
    await bot.send_message(chat_id=tg_chat_id, text=text)


def onebot_group_ban_task(
    event: OneBotGroupBanEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> SendTask:
    """创建发送到 Telegram 事件队列的群禁言任务。"""
    return SendTask(
        target=SendTarget.TELEGRAM,
        lane=SendLane.EVENT,
        send=partial(forward_onebot_group_ban_to_telegram, event, bot, gateway),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        label=f"onebot-group-ban:{event.group_id}:{event.user_id}",
    )


async def forward_onebot_group_member_to_telegram(
    event: OneBotGroupMemberEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> None:
    """把 OneBot 群成员加入或退出事件格式化为 Telegram 文本消息。"""
    tg_chat_id = await sql.get_tg_group(event.group_id)
    if tg_chat_id is None:
        baselog.warning("OneBot 群事件未配置转发目标: %s", event.group_id)
        return
    show_id = bool(await sql.get_id_show_enabled(tg_chat_id))
    name = await _onebot_member_name(
        gateway,
        event.group_id,
        event.user_id,
        no_cache=event.joined,
    )
    user = _event_member_text(name, event.user_id, show_id=show_id)
    action = "加入群聊" if event.joined else "退出群聊"
    await bot.send_message(chat_id=tg_chat_id, text=f"{user} {action}")


def onebot_group_member_task(
    event: OneBotGroupMemberEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> SendTask:
    """创建发送到 Telegram 事件队列的群成员变动任务。"""
    action = "increase" if event.joined else "decrease"
    return SendTask(
        target=SendTarget.TELEGRAM,
        lane=SendLane.EVENT,
        send=partial(forward_onebot_group_member_to_telegram, event, bot, gateway),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        label=f"onebot-group-{action}:{event.group_id}:{event.user_id}",
    )


async def forward_onebot_poke_to_telegram(
    event: OneBotPokeEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> None:
    """把 OneBot 群戳一戳事件格式化为 Telegram 文本消息。"""
    tg_chat_id = await sql.get_tg_group(event.group_id)
    if tg_chat_id is None:
        baselog.warning("OneBot 群事件未配置转发目标: %s", event.group_id)
        return
    show_id = bool(await sql.get_id_show_enabled(tg_chat_id))
    if event.user_id == event.target_id:
        user_name = await _onebot_member_name(
            gateway, event.group_id, event.user_id
        )
        target_name = None
    else:
        user_name, target_name = await asyncio.gather(
            _onebot_member_name(gateway, event.group_id, event.user_id),
            _onebot_member_name(gateway, event.group_id, event.target_id),
        )
    user = _event_member_text(user_name, event.user_id, show_id=show_id)
    target = (
        "自己"
        if event.user_id == event.target_id
        else _event_member_text(target_name, event.target_id, show_id=show_id)
    )
    await bot.send_message(
        chat_id=tg_chat_id,
        text=f"{user} {event.action} {target} {event.suffix}",
    )


def onebot_poke_task(
    event: OneBotPokeEvent,
    bot: ExtBot[None],
    gateway: QGateway,
) -> SendTask:
    """创建发送到 Telegram 事件队列的戳一戳任务。"""
    return SendTask(
        target=SendTarget.TELEGRAM,
        lane=SendLane.EVENT,
        send=partial(forward_onebot_poke_to_telegram, event, bot, gateway),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        label=f"onebot-poke:{event.group_id}:{event.user_id}:{event.target_id}",
    )


def onebot_recall_task(
    q_group_id: int,
    q_message_id: int,
    bot: ExtBot[None],
    *,
    tg_chat_id: int | None = None,
    tg_message_ids: tuple[int, ...] = (),
) -> SendTask:
    """创建与普通 OneBot 消息同队列、同重试策略的 Telegram 撤回任务。"""
    return SendTask(
        target=SendTarget.TELEGRAM,
        lane=SendLane.EVENT,
        send=partial(
            recall_onebot_message_from_telegram,
            q_group_id,
            q_message_id,
            bot,
            tg_chat_id=tg_chat_id,
            tg_message_ids=tg_message_ids,
        ),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        label=f"onebot-recall:{q_group_id}:{q_message_id}",
    )


async def finalize_onebot_forward(msg: OneBotMessage, bot: ExtBot[None]) -> None:
    """结束在途状态，并把等待中的撤回交给事件队列。"""
    from src.bus import message_bus

    key = (msg.group_id, msg.message_id)
    if key not in _pending_onebot_recalls:
        _active_onebot_forwards.discard(key)
        return
    if msg.tg_chat_id is None or not msg.tg_message_ids:
        _active_onebot_forwards.discard(key)
        _pending_onebot_recalls.discard(key)
        return
    await message_bus.put(
        onebot_recall_task(
            msg.group_id,
            msg.message_id,
            bot,
            tg_chat_id=msg.tg_chat_id,
            tg_message_ids=tuple(msg.tg_message_ids),
        )
    )
    _active_onebot_forwards.discard(key)
    _pending_onebot_recalls.discard(key)


async def _send_downloaded_media_individually(
    msg: OneBotMessage,
    bot: ExtBot[None],
    group_id: int,
    media: list[tuple[str, str, str]],
    contents: list[MediaFile],
    caption: str | None,
    reply_parameters: ReplyParameters | None,
) -> None:
    """按原顺序发送不能组成 Telegram 媒体组的已下载媒体。"""
    for index, (content, (kind, _, filename)) in enumerate(
        zip(contents, media, strict=True)
    ):
        is_gif = kind == "image" and _is_gif(content)
        upload_filename = f"{Path(filename).stem or 'animation'}.gif" if is_gif else filename
        upload = InputFile(
            content.file,
            filename=upload_filename,
            read_file_handle=False,
        )
        media_caption = caption if not msg.tg_message_ids else None
        media_reply = reply_parameters if not msg.tg_message_ids else None
        if is_gif:
            sent = await bot.send_animation(
                chat_id=group_id,
                animation=upload,
                caption=media_caption,
                show_caption_above_media=True,
                reply_parameters=media_reply,
            )
        elif kind == "image" and content.size <= PHOTO_LIMIT:
            sent = await bot.send_photo(
                chat_id=group_id,
                photo=upload,
                caption=media_caption,
                show_caption_above_media=True,
                reply_parameters=media_reply,
            )
        else:
            sent = await bot.send_document(
                chat_id=group_id,
                document=upload,
                caption=media_caption,
                reply_parameters=media_reply,
            )
        msg.tg_message_ids.append(sent.message_id)
        msg.next_media_index = index + 1


async def _prepare_media_caption(
    msg: OneBotMessage,
    bot: ExtBot[None],
    group_id: int,
    caption: str,
    reply_parameters: ReplyParameters | None,
) -> tuple[str | None, ReplyParameters | None]:
    if _utf16_length(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption, reply_parameters
    await _send_text_chunks(msg, bot, group_id, caption, reply_parameters)
    return None, None


async def _send_text_chunks(
    msg: OneBotMessage,
    bot: ExtBot[None],
    group_id: int,
    text: str,
    reply_parameters: ReplyParameters | None,
) -> None:
    chunks = _split_telegram_text(text)
    for index in range(msg.next_text_chunk_index, len(chunks)):
        sent = await bot.send_message(
            chat_id=group_id,
            text=chunks[index],
            reply_parameters=reply_parameters if index == 0 else None,
        )
        msg.tg_message_ids.append(sent.message_id)
        msg.next_text_chunk_index = index + 1


def _split_telegram_text(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for character in text:
        character_units = _utf16_length(character)
        if current and units + character_units > TELEGRAM_TEXT_LIMIT:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(character)
        units += character_units
    if current:
        chunks.append("".join(current))
    return chunks


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def forward_telegram_to_onebot(msg: TelegramMessage, gateway: QGateway) -> None:
    """将 Telegram 文本或图片通过 OneBot action 发送到绑定的 Onebot 群。"""
    group_id = await sql.get_q_group(msg.group_id)
    if group_id is None:
        baselog.warning("Telegram 群未配置转发目标: %s", msg.group_id)
        return
    if not await sql.get_tg_forward_enabled(msg.group_id):
        return
    if not msg.text and not msg.media:
        baselog.warning("Telegram 消息没有可转发的内容: %s", msg.message_ids)
        return

    batches: list[list[dict[str, Any]]]
    text = f"{msg.sender_name}:"
    if msg.forwarded_from is not None:
        text += f"\n转发自: {msg.forwarded_from}"
    if msg.text:
        text += f"\n{msg.text}"
    if msg.media:
        if msg.media_ids is None:
            msg.media_ids = media_cache.set_media_batch(
                tuple(attachment.content for attachment in msg.media),
                pinned=True,
            )
            msg.media_cache_pinned = True
        text_segment: dict[str, str | dict[str, str]] = {
            "type": "text",
            "data": {"text": text},
        }
        media_segments: list[dict[str, Any]] = []
        for attachment, media_id in zip(msg.media, msg.media_ids, strict=True):
            media_url = f"{config.onebot_media_url}/media/{media_id}"
            data = {"file": media_url}
            if attachment.kind == "file":
                data["name"] = attachment.content.filename
            media_segments.append({"type": attachment.kind, "data": data})
        if any(attachment.kind in {"record", "video"} for attachment in msg.media):
            # SnowLuma 的 video 和 record 都会丢失同 action 的文本
            batches = [[text_segment], media_segments]
        else:
            batches = [[text_segment, *media_segments]]
    else:
        batches = [[
            {"type": "text", "data": {"text": text}},
        ]]

    if msg.reply_message_id is not None:
        reply_mapping = await sql.get_q_message(msg.group_id, msg.reply_message_id)
        if reply_mapping is not None:
            batches[0].insert(
                0,
                {"type": "reply", "data": {"id": str(reply_mapping.q_message_ids[-1])}},
            )

    for index in range(msg.next_onebot_batch, len(batches)):
        try:
            message_id = await gateway.send_group_message(
                group_id=group_id,
                message=batches[index],
            )
        except OneBotConnectionError:
            raise
        except Exception as error:
            raise OneBotSendError from error
        msg.q_message_ids.append(message_id)
        msg.next_onebot_batch = index + 1

    try:
        await sql.set_message_mapping(
            q_group_id=group_id,
            q_message_ids=tuple(msg.q_message_ids),
            tg_chat_id=msg.group_id,
            tg_message_ids=msg.message_ids,
            tg_user_id=msg.user_id,
        )
    except Exception:  # noqa: BLE001
        baselog.exception("Onebot 消息发送成功，但消息映射保存失败")


async def finalize_telegram_message(msg: TelegramMessage) -> None:
    """任务结束后归还队列预算，并关闭未被缓存接管的文件。"""
    if msg.queue_bytes:
        await media_queue_budget.release(msg.queue_bytes)
        msg.queue_bytes = 0
    if msg.media_ids is not None and msg.media_cache_pinned:
        media_cache.release_media_batch(msg.media_ids)
        msg.media_cache_pinned = False
    elif msg.media_ids is None:
        for attachment in msg.media:
            attachment.content.close()


def _is_mp4(content: MediaFile) -> bool:
    """Telegram 只有 MP4 能作为可播放视频，其它容器按文件发送。"""
    return _media_mime(content) == "video/mp4"


def _is_gif(content: MediaFile) -> bool:
    """通过文件签名识别 Onebot image 段中的动态 GIF。"""
    return _media_mime(content) == "image/gif"


def _media_mime(content: MediaFile) -> str | None:
    """读取公开格式的文件签名，不信任远端 MIME 和文件扩展名。"""
    position = content.file.tell()
    try:
        content.file.seek(0)
        kind = filetype.guess(content.file.read(261))
        return kind.mime if kind is not None else None
    finally:
        content.file.seek(position)


def _onebot_failure_action(error: Exception) -> FailureAction:
    """把 OneBot 连接状态和业务失败映射为通用总线动作。"""
    if isinstance(error, OneBotConnectionError):
        return FailureAction.DEFER
    if isinstance(error, OneBotSendError):
        return FailureAction.RETRY
    return FailureAction.DROP


def _telegram_failure_action(error: Exception) -> FailureAction:
    """只重试结果明确未成功的错误，避免超时后重复发送。"""
    if isinstance(error, (MediaTooLargeError, TimedOut)):
        return FailureAction.DROP
    return FailureAction.RETRY


async def _disable_forwarding(
    msg: TelegramMessage,
    bot: Bot,
    gateway: QGateway,
    error: Exception,
) -> None:
    """向来源群提醒最终失败；业务发送耗尽时额外关闭转发。"""
    if isinstance(error, OneBotSendError):
        await sql.set_tg_forward_enabled(msg.group_id, False)
        q_group_id = await sql.get_q_group(msg.group_id)
        text = (
            "转发到 Onebot 连续失败 3 次，已自动关闭转发。"
            "请排查后使用 /forward on 重新开启。"
        )
        enqueue_bridge_notice(
            partial(bot.send_message, chat_id=msg.group_id, text=text),
            gateway,
            q_group_id=q_group_id,
            text=text,
        )
        return

    enqueue_telegram_notice(
        partial(
            bot.send_message,
            chat_id=msg.group_id,
            text="消息发送到 Onebot 失败，请稍后重试。",
        )
    )


async def _notify_onebot_telegram_failure(
    msg: OneBotMessage,
    gateway: QGateway,
    error: Exception,
) -> None:
    """Telegram 任务耗尽后只向来源 Onebot 群发送失败提示。"""
    text = (
        str(error)
        if isinstance(error, MediaTooLargeError)
        else (
            "消息发送到 Telegram 超时，发送结果未知；为避免重复发送未自动重试，"
            "请检查 Telegram 群。"
            if isinstance(error, TimedOut)
            else "消息转发到 Telegram 连续失败 3 次，请稍后重试。"
        )
    )
    enqueue_onebot_notice(
        gateway,
        q_group_id=msg.group_id,
        text=text,
    )


def telegram_forward_task(
    msg: TelegramMessage,
    gateway: QGateway,
    bot: Bot,
) -> SendTask:
    """创建目标为 Onebot、携带 OneBot 重试策略的通用发送任务。"""
    return SendTask(
        target=SendTarget.ONEBOT,
        send=partial(forward_telegram_to_onebot, msg, gateway),
        failure_action=_onebot_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        on_failed=partial(_disable_forwarding, msg, bot, gateway),
        finalize=partial(finalize_telegram_message, msg),
        label=f"telegram-to-onebot:{msg.group_id}:{msg.message_ids}",
    )


async def prepare_telegram_forward(
    msg: TelegramMessage,
    gateway: QGateway,
    bot: Bot,
) -> None:
    """串行规范化视频，完成后把发送任务交给 Onebot 队列。"""
    from src.bus import message_bus

    for attachment in msg.media:
        if attachment.processing == "video":
            await track_conversion(
                "video",
                normalize_video_for_onebot(
                    attachment.content,
                    size_limit=TELEGRAM_VIDEO_LIMIT,
                ),
            )
        elif attachment.processing == "sticker_static":
            await track_conversion(
                "sticker_static",
                static_sticker_to_png(
                    attachment.content,
                    size_limit=TELEGRAM_VIDEO_LIMIT,
                ),
            )
        elif attachment.processing == "sticker_tgs":
            await track_conversion(
                "sticker_tgs",
                tgs_sticker_to_gif(attachment.content),
            )
        elif attachment.processing == "sticker_video":
            await track_conversion(
                "sticker_video",
                video_sticker_to_gif(
                    attachment.content,
                    size_limit=TELEGRAM_VIDEO_LIMIT,
                ),
            )
    normalized_size = sum(attachment.content.size for attachment in msg.media)
    if normalized_size < msg.queue_bytes:
        await media_queue_budget.release(msg.queue_bytes - normalized_size)
        msg.queue_bytes = normalized_size
    await message_bus.put(telegram_forward_task(msg, gateway, bot))


async def _notify_processing_failure(
    msg: TelegramMessage,
    bot: Bot,
    error: Exception,
) -> None:
    """预处理已脱离 Update handler，失败时通过发送队列通知原 Telegram 群。"""
    enqueue_telegram_notice(
        partial(
            bot.send_message,
            chat_id=msg.group_id,
            text=f"媒体处理失败：{error}",
        )
    )


def telegram_processing_task(
    msg: TelegramMessage,
    gateway: QGateway,
    bot: Bot,
) -> ProcessingTask:
    """创建视频预处理任务；失败时释放尚未转交发送队列的媒体。"""
    return ProcessingTask(
        run=partial(prepare_telegram_forward, msg, gateway, bot),
        cleanup=partial(finalize_telegram_message, msg),
        on_error=partial(_notify_processing_failure, msg, bot),
        label=f"telegram-media:{msg.group_id}:{msg.message_ids}",
    )


def onebot_forward_task(
    msg: OneBotMessage,
    bot: ExtBot[None],
    client: httpx.AsyncClient,
    gateway: QGateway,
) -> SendTask:
    """创建目标为 Telegram、失败三次后仅通知 Onebot 的通用任务。"""
    return SendTask(
        target=SendTarget.TELEGRAM,
        send=partial(forward_onebot_to_telegram, msg, bot, client, gateway),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        on_failed=partial(_notify_onebot_telegram_failure, msg, gateway),
        finalize=partial(finalize_onebot_forward, msg, bot),
        label=f"onebot-to-telegram:{msg.group_id}:{msg.message_id}",
    )
