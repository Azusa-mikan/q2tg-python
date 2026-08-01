"""Telegram 命令、文本和媒体 Update 的入口处理器。"""

import asyncio
from functools import partial
from pathlib import Path

import httpx
from telegram import (
    ChatMember,
    Message,
    MessageOrigin,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bus import message_bus
from src.config import config
from src.forwarding import telegram_forward_task, telegram_processing_task
from src.media import MediaFile, media_item_budget, media_queue_budget
from src.messages import TelegramMedia, TelegramMessage
from src.notice import enqueue_bridge_notice
from src.processing import media_processor
from src.qbot import q_gateway
from src.sql import sql

TELEGRAM_DOWNLOAD_LIMIT = 20_000_000
TELEGRAM_VIDEO_LIMIT = TELEGRAM_DOWNLOAD_LIMIT
TELEGRAM_ALBUM_LIMIT = 10
TELEGRAM_ALBUM_BYTES_LIMIT = 100_000_000
DOWNLOAD_CHUNK_SIZE = 256 * 1024


def forward_origin_name(origin: MessageOrigin | None) -> str | None:
    """提取 Telegram 转发来源的可见名称。"""
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name.strip() or None
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title or (
            f"@{origin.sender_chat.username}" if origin.sender_chat.username else None
        )
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title or (
            f"@{origin.chat.username}" if origin.chat.username else None
        )
    return None


def command(cmd: str):
    """给方法附加命令处理器元数据，实际 Handler 在 get_handlers 中创建。"""

    def decorator(fn):
        fn._ptb_handler = ("command", cmd)
        return fn
    return decorator


def message(filter: filters.BaseFilter):
    """给方法附加消息过滤器元数据。"""

    def decorator(fn):
        fn._ptb_handler = ("message", filter)
        return fn
    return decorator


class TGhandlers:
    """Telegram 命令、文本和媒体入口集合。

    Handler 的职责是尽快把 Telegram Update 转换为内部 TelegramMessage 并入队。实际
    OneBot 发送由消息消费者完成，避免 Telegram 更新处理逻辑直接依赖 WebSocket。
    """

    def __init__(self) -> None:
        # Telegram 相册会拆成多个 Update；以 media_group_id 暂存到同一列表。
        self._albums: dict[str, list[Message]] = {}

        # 每个相册只创建一个延迟 flush 任务，后续图片只追加到列表。
        self._album_tasks: dict[str, asyncio.Task[None]] = {}

        # 由 SnowLuma WebSocket 会话注入和关闭，handlers 不拥有该客户端的生命周期。
        self.download_client: httpx.AsyncClient | None = None

    @command("start")
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """提供一个简单的存活确认命令。"""
        if not (msg := update.message):
            return
        await msg.reply_text("Bot 已启动！")

    @command("bind")
    async def bind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """由管理员在当前 Telegram 群绑定一个 OneBot 群号。"""
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return
        if chat.type not in {"group", "supergroup"}:
            # 私聊没有可作为桥接目标的群 chat_id，因此拒绝执行。
            await msg.reply_text("请在 Telegram 群聊中使用此命令")
            return
        if user.id != config.tgbot_admin:
            await msg.reply_text("只有管理员可以绑定群聊")
            return
        args = context.args or []
        if len(args) != 1:
            await msg.reply_text("用法：/bind <OneBot 群号>")
            return

        try:
            q_group_id = int(args[0])
            # Sql 会校验正整数和一对一冲突，不在 handler 中重复规则。
            await sql.bind_group(q_group_id, chat.id)
        except ValueError as error:
            await msg.reply_text(str(error))
            return

        enqueue_bridge_notice(
            partial(msg.reply_text, f"已绑定 Telegram 群与 Onebot 群 {q_group_id}"),
            q_gateway,
            q_group_id=q_group_id,
            text=f"已绑定 Telegram 群与 Onebot 群 {q_group_id}",
        )

    @command("unbind")
    async def unbind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """解除当前 Telegram 群的双向绑定。"""
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return
        if chat.type not in {"group", "supergroup"}:
            await msg.reply_text("请在 Telegram 群聊中使用此命令")
            return
        if user.id != config.tgbot_admin:
            await msg.reply_text("只有管理员可以解除绑定")
            return

        q_group_id = await sql.unbind_tg_group(chat.id)
        if q_group_id is None:
            await msg.reply_text("当前群聊尚未绑定 OneBot 群")
            return

        enqueue_bridge_notice(
            partial(msg.reply_text, f"已解除 Telegram 群与 Onebot 群 {q_group_id} 的绑定"),
            q_gateway,
            q_group_id=q_group_id,
            text=f"已解除 Telegram 群与 Onebot 群 {q_group_id} 的绑定",
        )

    @command("forward")
    async def forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """查询或设置当前 Telegram 群到 Onebot 的转发开关。"""
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return
        if chat.type not in {"group", "supergroup"}:
            await msg.reply_text("请在 Telegram 群聊中使用此命令")
            return

        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in {ChatMember.ADMINISTRATOR, ChatMember.OWNER}:
            await msg.reply_text("只有群聊管理员可以设置转发开关")
            return

        args = context.args or []
        if len(args) > 1 or (args and args[0].lower() not in {"on", "off"}):
            await msg.reply_text("用法：/forward [on|off]")
            return
        if not args:
            enabled = await sql.get_tg_forward_enabled(chat.id)
            if enabled is None:
                await msg.reply_text("当前群聊尚未绑定 OneBot 群")
                return
            q_group_id = await sql.get_q_group(chat.id)
            enqueue_bridge_notice(
                partial(
                    msg.reply_text,
                    f"当前 Telegram → Onebot 转发已{'开启' if enabled else '关闭'}",
                ),
                q_gateway,
                q_group_id=q_group_id,
                text=f"当前 Telegram → Onebot 转发已{'开启' if enabled else '关闭'}",
            )
            return

        enabled = args[0].lower() == "on"
        q_group_id = await sql.get_q_group(chat.id)
        if q_group_id is None:
            await msg.reply_text("当前群聊尚未绑定 OneBot 群")
            return
        if not await sql.set_tg_forward_enabled(chat.id, enabled):
            await msg.reply_text("当前群聊尚未绑定 OneBot 群")
            return
        enqueue_bridge_notice(
            partial(
                msg.reply_text,
                f"Telegram → Onebot 转发已{'开启' if enabled else '关闭'}",
            ),
            q_gateway,
            q_group_id=q_group_id,
            text=f"Telegram → Onebot 转发已{'开启' if enabled else '关闭'}",
        )

    @command("id_show")
    async def id_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """查询或设置 Onebot 用户及 @ 对象是否在 Telegram 显示数字 ID。"""
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return
        if chat.type not in {"group", "supergroup"}:
            await msg.reply_text("请在 Telegram 群聊中使用此命令")
            return

        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in {ChatMember.ADMINISTRATOR, ChatMember.OWNER}:
            await msg.reply_text("只有群聊管理员可以设置 ID 显示")
            return

        args = context.args or []
        if len(args) > 1 or (args and args[0].lower() not in {"on", "off"}):
            await msg.reply_text("用法：/id_show [on|off]")
            return
        if not args:
            enabled = await sql.get_id_show_enabled(chat.id)
            if enabled is None:
                await msg.reply_text("当前群聊尚未绑定 OneBot 群")
                return
            await msg.reply_text(f"Onebot 用户及 @ 对象 ID 显示已{'开启' if enabled else '关闭'}")
            return

        enabled = args[0].lower() == "on"
        if not await sql.set_id_show_enabled(chat.id, enabled):
            await msg.reply_text("当前群聊尚未绑定 OneBot 群")
            return
        await msg.reply_text(f"Onebot 用户及 @ 对象 ID 显示已{'开启' if enabled else '关闭'}")

    @command("undo")
    async def undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """撤回被回复消息在 Telegram 和 OneBot 两侧的对应消息。"""
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return
        if chat.type not in {"group", "supergroup"}:
            await msg.reply_text("请在 Telegram 群聊中使用此命令")
            return
        target = msg.reply_to_message
        if target is None:
            await msg.reply_text("请回复需要撤回的消息后使用 /undo")
            return
        mapping = await sql.get_q_message(chat.id, target.message_id)
        if mapping is None:
            await msg.reply_text("未找到该消息的跨平台映射")
            return
        member = await context.bot.get_chat_member(chat.id, user.id)
        is_admin = member.status in {ChatMember.ADMINISTRATOR, ChatMember.OWNER}
        # target 通常是 Bot 发出的转发消息，因此必须使用映射中保存的原始 TG 用户 ID。
        is_owner = mapping.tg_user_id == user.id
        if not is_admin and not is_owner:
            await msg.reply_text("非群聊管理员只能撤回自己的消息")
            return

        failures = 0
        for message_id in mapping.q_message_ids:
            try:
                # 目标消息由群主发送、机器人仅为管理员时，Onebot 可能返回成功，
                # 但群聊权限仍会阻止实际撤回。
                await q_gateway.delete_message(message_id)
            except RuntimeError:
                failures += 1
        if failures:
            await msg.reply_text("OneBot 撤回失败，消息可能超过两分钟或机器人权限不足")
            return
        for index in range(0, len(mapping.tg_message_ids), 100):
            await context.bot.delete_messages(
                chat_id=mapping.tg_chat_id,
                message_ids=mapping.tg_message_ids[index : index + 100],
            )
        await msg.delete()

    @message(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND)
    async def receive_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """把 Telegram 群中的非命令文本转换为 TelegramMessage。"""
        if not (msg := update.message):
            return
        if not await sql.get_tg_forward_enabled(msg.chat_id):
            return
        user_id = msg.from_user.id if msg.from_user is not None else 0
        sender_name = msg.from_user.full_name if msg.from_user is not None else f"Telegram用户 {user_id}"
        message = TelegramMessage(
            # 纯文本只有一个 Telegram message_id，仍使用元组统一映射结构。
            message_ids=(msg.message_id,),
            group_id=msg.chat_id,
            user_id=user_id,
            sender_name=sender_name,
            text=msg.text,
            forwarded_from=forward_origin_name(getattr(msg, "forward_origin", None)),
            reply_message_id=(
                msg.reply_to_message.message_id if msg.reply_to_message is not None else None
            ),
        )
        await message_bus.put(telegram_forward_task(message, q_gateway, context.bot))

    @message(
        filters.ChatType.GROUPS
        & (
            filters.PHOTO
            | filters.VIDEO
            | filters.VOICE
            | filters.Document.ALL
            | filters.Sticker.ALL
        )
    )
    async def receive_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理单个图片、视频、语音或文件，或按 media_group_id 收集媒体组。"""
        if not (msg := update.message):
            return
        if msg.media_group_id is None:
            try:
                await self._enqueue_media([msg])
            except ValueError as error:
                await msg.reply_text(str(error))
            return

        album = self._albums.setdefault(msg.media_group_id, [])
        album.append(msg)
        if msg.media_group_id not in self._album_tasks:
            # PTB Application 创建任务后会跟踪其异常和关闭行为。
            task = context.application.create_task(
                self._flush_album(msg.media_group_id),
                update=update,
            )
            self._album_tasks[msg.media_group_id] = task

    async def _flush_album(self, media_group_id: str) -> None:
        """等待短聚合窗口后，把同一相册作为一条内部消息处理。"""
        try:
            # Telegram 没有明确的“相册结束”事件，只能通过短暂静默判断收集完成。
            await asyncio.sleep(0.75)
            messages = self._albums.pop(media_group_id, [])
            if messages:
                try:
                    await self._enqueue_media(messages)
                except ValueError as error:
                    await messages[0].reply_text(str(error))
        finally:
            # 取消可能发生在聚合窗口内，此时还没有 pop 相册消息。
            self._albums.pop(media_group_id, None)
            self._album_tasks.pop(media_group_id, None)

    async def _enqueue_media(self, messages: list[Message]) -> None:
        """下载一组 Telegram 媒体，取得资源预算后放入消息队列。

         Telegram 的 Message.photo 是同一张照片的多个尺寸，不是多张照片；这里
        选择最后一个最大尺寸。video、voice 和 document 分别映射为 Onebot 的
        video、record 和 file。函数成功入队后，MediaFile 所有权交给转发任务；
        此前的异常或取消路径由本函数清理。
        """
        # Update 到达次序不一定稳定，按 message_id 恢复用户发送的相册顺序。
        messages.sort(key=lambda message: message.message_id)
        if len(messages) > TELEGRAM_ALBUM_LIMIT:
            raise ValueError("Telegram 媒体组超过 10 项上限")

        first = messages[0]
        if not await sql.get_tg_forward_enabled(first.chat_id):
            return
        user_id = first.from_user.id if first.from_user is not None else 0
        sender_name = first.from_user.full_name if first.from_user is not None else f"Telegram用户 {user_id}"
        sources = []
        for message in messages:
            if (sticker := getattr(message, "sticker", None)) is not None:
                if sticker.is_animated:
                    if sticker.thumbnail is None:
                        raise ValueError("Telegram 动态贴纸缺少可用缩略图，无法转发")
                    sources.append(
                        (
                            "image",
                            sticker.thumbnail,
                            TELEGRAM_DOWNLOAD_LIMIT,
                            "sticker_static",
                        )
                    )
                elif sticker.is_video:
                    sources.append(
                        (
                            "image",
                            sticker,
                            TELEGRAM_DOWNLOAD_LIMIT,
                            "sticker_video",
                        )
                    )
                else:
                    sources.append(
                        (
                            "image",
                            sticker,
                            TELEGRAM_DOWNLOAD_LIMIT,
                            "sticker_static",
                        )
                    )
            elif message.video is not None:
                sources.append(("video", message.video, TELEGRAM_DOWNLOAD_LIMIT, "video"))
            elif (voice := getattr(message, "voice", None)) is not None:
                sources.append(("record", voice, TELEGRAM_DOWNLOAD_LIMIT, "none"))
            elif message.photo:
                sources.append(("image", message.photo[-1], TELEGRAM_DOWNLOAD_LIMIT, "none"))
            elif (document := message.document) is not None:
                sources.append(("file", document, TELEGRAM_DOWNLOAD_LIMIT, "none"))

        declared_size = sum(source.file_size or limit for _, source, limit, _ in sources)
        if any(
            source.file_size is not None and source.file_size > limit
            for _, source, limit, _ in sources
        ):
            raise ValueError("Telegram 媒体超过 20 MB，无法转发")
        if declared_size > TELEGRAM_ALBUM_BYTES_LIMIT:
            raise ValueError("Telegram 媒体组超过 100 MB 上限")

        if self.download_client is None:
            raise RuntimeError("Telegram 媒体下载客户端尚未启动")

        media: list[TelegramMedia] = []
        # 先按最坏情况占用队列预算，再开始网络下载。否则多个并发相册都可能先
        # 下载完几十 MB，最后才发现预算不足，预算就失去了限制临时存储的意义。
        reserved_bytes = sum(limit for _, _, limit, _ in sources)
        await media_queue_budget.acquire(reserved_bytes)
        reserved_items = len(sources)
        item_budget_acquired = False
        try:
            # 为 OneBot -> Telegram 的消费者下载保留一个临时文件槽位，避免死锁。
            await media_item_budget.acquire(len(sources), reserve=1)
            item_budget_acquired = True
            for kind, source, size_limit, processing in sources:
                # get_file 获取至少一小时有效的下载 URL 和更准确的文件元数据。
                file = await source.get_file()
                if file.file_size is not None and file.file_size > size_limit:
                    raise ValueError("Telegram 媒体超过 20 MB，无法转发")
                if not file.file_path:
                    raise RuntimeError("Telegram 媒体缺少下载地址")

                source_filename = getattr(source, "file_name", None)
                filename = source_filename or Path(file.file_path).name
                if not filename:
                    filename = {
                        "image": "image.jpg",
                        "record": "voice.ogg",
                        "video": "video.mp4",
                    }.get(kind, "file")
                media_type = getattr(source, "mime_type", None)
                if not media_type:
                    media_type = {
                        "image": "image/jpeg",
                        "record": "audio/ogg",
                        "video": "video/mp4",
                    }.get(kind, "application/octet-stream")
                content = MediaFile.create_reserved(filename=filename, media_type=media_type)
                # create_reserved 消耗的是上面批量取得的名额。每成功创建一个，
                # reserved_items 就减少一个，异常清理时只归还尚未使用的名额。
                reserved_items -= 1
                try:
                    try:
                        async with self.download_client.stream("GET", file.file_path) as response:
                            response.raise_for_status()
                            # 先用响应头提前拒绝，再在读取 chunk 时核对实际总大小。
                            content_length = response.headers.get("content-length")
                            if content_length is not None and int(content_length) > size_limit:
                                raise ValueError("Telegram 媒体超过 20 MB，无法转发")
                            async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_SIZE):
                                if content.size + len(chunk) > size_limit:
                                    raise ValueError("Telegram 媒体超过 20 MB，无法转发")
                                content.write(chunk)
                    except httpx.HTTPError:
                        # Telegram 文件 URL 包含 Bot Token，不能让 HTTPX 异常把 URL
                        # 原样带进 PTB 的 traceback 日志。
                        raise RuntimeError("Telegram 媒体下载失败") from None
                    content.rewind()
                    # 下载完成后游标位于文件末尾，入队前重置以供后续读取。
                    media.append(
                        TelegramMedia(
                            kind=kind,
                            content=content,
                            processing=processing,
                        )
                    )
                except BaseException:
                    content.close()
                    raise

            total_size = sum(attachment.content.size for attachment in media)
            if total_size > TELEGRAM_ALBUM_BYTES_LIMIT:
                raise ValueError("Telegram 媒体组超过 100 MB 上限")
            needs_processing = any(
                attachment.processing != "none" for attachment in media
            )
            if not needs_processing:
                # 图片大小不会再改变，可以立即归还下载前的保守预留。
                await media_queue_budget.release(reserved_bytes - total_size)
                reserved_bytes = total_size
            try:
                message = TelegramMessage(
                        message_ids=tuple(message.message_id for message in messages),
                        group_id=first.chat_id,
                        user_id=user_id,
                        sender_name=sender_name,
                        text=next((message.caption for message in messages if message.caption), None),
                        forwarded_from=next(
                            (
                                name
                                for message in messages
                                if (
                                    name := forward_origin_name(
                                        getattr(message, "forward_origin", None)
                                    )
                                )
                                is not None
                            ),
                            None,
                        ),
                        reply_message_id=next(
                            (
                                message.reply_to_message.message_id
                                for message in messages
                                if message.reply_to_message is not None
                            ),
                            None,
                        ),
                        # 媒体组 caption 通常只附在其中一项，取第一条非空文本。
                        media=tuple(media),
                        # 视频转码可能改变大小，预处理完成前保留最坏情况预算。
                        queue_bytes=reserved_bytes,
                    )
                if needs_processing:
                    task = telegram_processing_task(message, q_gateway, first.get_bot())
                    if not media_processor.submit(task):
                        await task.cleanup()
                        reserved_bytes = 0
                        raise ValueError("Telegram 媒体处理队列已满，请稍后重试")
                else:
                    await message_bus.put(
                        telegram_forward_task(message, q_gateway, first.get_bot())
                    )
            except BaseException:
                await media_queue_budget.release(total_size)
                reserved_bytes = 0
                raise
        except BaseException:
            # BaseException 包含任务取消；关停时取消相册任务也必须关闭临时文件，
            # 并归还已经取得但尚未使用的两类预算。
            for attachment in media:
                attachment.content.close()
            if item_budget_acquired and reserved_items:
                media_item_budget.release(reserved_items)
            if reserved_bytes:
                await media_queue_budget.release(reserved_bytes)
            raise

        if not media:
            return

    def get_handlers(self):
        """扫描继承层次中的装饰器元数据并创建 PTB Handler。

        运行时实例是 TGBot，而方法定义在父类 TGhandlers 中，因此必须遍历完整
        MRO，不能只查看 vars(type(self))。子类同名方法会覆盖父类注册项。
        """
        handlers: list[BaseHandler] = []
        methods = {}
        # 从基类到子类更新字典，确保最后保留最具体的实现。
        for cls in reversed(type(self).__mro__):
            methods.update(vars(cls))

        for fn in methods.values():
            info = getattr(fn, "_ptb_handler", None)
            if info is None:
                continue
            kind, arg = info
            bound = fn.__get__(self, self.__class__)
            # 装饰器只保存元数据；这里才把未绑定函数变成当前实例的绑定方法。
            if kind == "command":
                handlers.append(CommandHandler(arg, bound))
            elif kind == "message":
                handlers.append(MessageHandler(arg, bound))
        return handlers
