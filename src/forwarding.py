from __future__ import annotations

"""两个平台之间的消息转换与发送。"""

from functools import partial
from mimetypes import guess_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from telegram import (
    Bot,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    ReplyParameters,
)
from telegram.ext import ExtBot

from src.config import config
from src.log import baselog
from src.media import MediaFile, media_cache, media_queue_budget
from src.messages import (
    FailureAction,
    MediaTooLargeError,
    OneBotConnectionError,
    OneBotMessage,
    OneBotSendError,
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
from src.sql import sql
from src.sticker import static_sticker_to_png, video_sticker_to_gif
from src.video import normalize_video_for_onebot

if TYPE_CHECKING:
    from src.qbot import QGateway

PHOTO_LIMIT = 10_000_000
ONEBOT_MEDIA_LIMIT = 20_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
MAX_SEND_ATTEMPTS = 3
TELEGRAM_VIDEO_LIMIT = 20_000_000


def onebot_message_text(message: list[dict[Any, Any]]) -> str:
    """按原顺序拼接 OneBot 消息中的 text segment。"""
    parts = []
    for segment in message:
        if segment.get("type") == "text":
            data = segment.get("data")
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def onebot_message_media(
    message: list[dict[Any, Any]],
) -> tuple[list[tuple[str, str, str]], int]:
    """提取图片、视频和文件，并统计缺少标准下载 URL 的视频。"""
    media = []
    unavailable_videos = 0
    for segment in message:
        kind = segment.get("type")
        if kind not in {"file", "image", "video"}:
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            if kind == "video":
                unavailable_videos += 1
            continue
        url = data.get("url")
        if not isinstance(url, str) or not url:
            if kind == "video":
                unavailable_videos += 1
            continue
        filename = _onebot_media_filename(data.get("file"), kind)
        media.append((kind, url, filename))
    return media, unavailable_videos


def _onebot_media_filename(file: Any, kind: str) -> str:
    """把入站 data.file 作为文件名，并移除不适合上传文件名的字符。"""
    if isinstance(file, str):
        filename = file.strip().replace("\x00", "").replace("\r", "").replace("\n", "")
        # file 在入站消息中是文件名，不是本地路径；分隔符只做安全替换。
        filename = filename.replace("/", "_").replace("\\", "_")
        if filename and filename not in {".", ".."}:
            return filename
    if kind == "video":
        return "video.mp4"
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
) -> None:
    """将 OneBot 文本和图片按顺序发送到绑定的 Telegram 群。"""
    group_id = await sql.get_tg_group(msg.group_id)
    if group_id is None:
        baselog.warning("OneBot 群未配置转发目标: %s", msg.group_id)
        return

    text = onebot_message_text(msg.message)
    sender_name = msg.sender_name
    if msg.sender_name_is_fallback and not await sql.get_id_show_enabled(group_id):
        sender_name = "OneBot 用户"
    media, unavailable_videos = onebot_message_media(msg.message)
    if not text and not media and not unavailable_videos:
        baselog.warning("OneBot 消息没有可转发的内容: %s", msg.message_id)
        return

    caption = f"{sender_name}:\n{text}" if text else f"{sender_name}:"
    reply_parameters = None
    if msg.reply_message_id is not None:
        reply_mapping = await sql.get_tg_message(msg.group_id, msg.reply_message_id)
        if reply_mapping is not None and reply_mapping.tg_message_ids:
            reply_parameters = ReplyParameters(
                message_id=reply_mapping.tg_message_ids[0],
                allow_sending_without_reply=True,
            )

    if not media:
        if unavailable_videos and not text:
            caption = f"{sender_name}:\n[视频无法转发：缺少下载地址]"
        if not msg.tg_message_ids:
            sent = await bot.send_message(
                chat_id=group_id,
                text=caption,
                reply_parameters=reply_parameters,
            )
            msg.tg_message_ids.append(sent.message_id)
    elif (
        not msg.tg_message_ids
        and msg.next_media_index == 0
        and 2 <= len(media) <= 10
        and all(kind == "image" for kind, _, _ in media)
    ):
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
                    caption,
                    reply_parameters,
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
                        caption=caption if index == 0 else None,
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
                        caption=caption if index == len(contents) - 1 else None,
                    )
                    for index, (content, (_, _, filename)) in enumerate(
                        zip(contents, media, strict=True)
                    )
                ]
            if not as_animations:
                sent_messages = await bot.send_media_group(
                    chat_id=group_id,
                    media=album,
                    reply_parameters=reply_parameters,
                )
                msg.tg_message_ids.extend(sent.message_id for sent in sent_messages)
                msg.next_media_index = len(media)
        finally:
            for content in contents:
                content.close()
    else:
        for index in range(msg.next_media_index, len(media)):
            kind, url, filename = media[index]
            content = await download_media(
                client,
                url,
                filename=filename,
                kind=kind,
            )
            try:
                is_gif = kind == "image" and _is_gif(content)
                upload_filename = (
                    f"{Path(filename).stem or 'animation'}.gif" if is_gif else filename
                )
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
                elif kind == "video" and _is_mp4(content):
                    sent = await bot.send_video(
                        chat_id=group_id,
                        video=upload,
                        caption=media_caption,
                        show_caption_above_media=True,
                        supports_streaming=True,
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
            finally:
                content.close()
        if not msg.tg_message_ids:
            sent = await bot.send_message(
                chat_id=group_id,
                text=f"{caption}\n[视频无法转发：缺少下载地址]",
                reply_parameters=reply_parameters,
            )
            msg.tg_message_ids.append(sent.message_id)

    # 远端发送已经完成，本地映射失败不能触发 Telegram 重发。
    try:
        await sql.set_message_mapping(
            q_group_id=msg.group_id,
            q_message_id=msg.message_id,
            tg_chat_id=group_id,
            tg_message_ids=tuple(msg.tg_message_ids),
            q_user_id=msg.user_id,
        )
    except Exception:  # noqa: BLE001
        baselog.exception("Telegram 消息发送成功，但消息映射保存失败")


async def _send_downloaded_media_individually(
    msg: OneBotMessage,
    bot: ExtBot[None],
    group_id: int,
    media: list[tuple[str, str, str]],
    contents: list[MediaFile],
    caption: str,
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
    text = f"{msg.sender_name}:\n"
    if msg.forwarded_from is not None:
        text += f"转发自: {msg.forwarded_from}\n"
    text += msg.text or ""
    if msg.media:
        if msg.media_ids is None:
            msg.media_ids = media_cache.set_media_batch(
                tuple(attachment.content for attachment in msg.media)
            )
        text_segment = {
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
        if any(attachment.kind == "video" for attachment in msg.media):
            # SnowLuma 不发送与 video 位于同一 action 的 text，必须拆成两条消息。
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
                {"type": "reply", "data": {"id": str(reply_mapping.q_message_id)}},
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
            q_message_id=msg.q_message_ids[-1],
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
    if msg.media_ids is None:
        for attachment in msg.media:
            attachment.content.close()


def _is_mp4(content: MediaFile) -> bool:
    """Telegram 只有 MP4 能作为可播放视频，其它容器按文件发送。"""
    guessed_type, _ = guess_type(Path(content.filename).name)
    return content.media_type == "video/mp4" or guessed_type == "video/mp4"


def _is_gif(content: MediaFile) -> bool:
    """通过 MIME 或文件签名识别 Onebot image 段中的动态 GIF。"""
    if content.media_type.lower() == "image/gif":
        return True
    position = content.file.tell()
    try:
        content.file.seek(0)
        return content.file.read(6) in {b"GIF87a", b"GIF89a"}
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
    """Telegram 发送失败统一重试，最多次数由任务配置决定。"""
    if isinstance(error, MediaTooLargeError):
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
        else "消息转发到 Telegram 连续失败 3 次，请稍后重试。"
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
            await normalize_video_for_onebot(
                attachment.content,
                size_limit=TELEGRAM_VIDEO_LIMIT,
            )
        elif attachment.processing == "sticker_static":
            await static_sticker_to_png(
                attachment.content,
                size_limit=TELEGRAM_VIDEO_LIMIT,
            )
        elif attachment.processing == "sticker_video":
            await video_sticker_to_gif(
                attachment.content,
                size_limit=TELEGRAM_VIDEO_LIMIT,
            )
    normalized_size = sum(attachment.content.size for attachment in msg.media)
    if normalized_size > msg.queue_bytes:
        raise RuntimeError("媒体规范化结果超过预留队列预算")
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
        send=partial(forward_onebot_to_telegram, msg, bot, client),
        failure_action=_telegram_failure_action,
        max_attempts=MAX_SEND_ATTEMPTS,
        on_failed=partial(_notify_onebot_telegram_failure, msg, gateway),
        label=f"onebot-to-telegram:{msg.group_id}:{msg.message_id}",
    )
