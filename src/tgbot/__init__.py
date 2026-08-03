"""把 python-telegram-bot 的生命周期嵌入 FastAPI 的 asyncio 事件循环。"""

import asyncio

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder

from src.log import baselog

from .handlers import TGhandlers

BOT_COMMANDS = (
    BotCommand("start", "查看 Bot 运行状态"),
    BotCommand("status", "查看 Q2TG 运行状态"),
    BotCommand("bind", "绑定当前 Telegram 群与 Onebot 群"),
    BotCommand("unbind", "解除当前群的 Onebot 群绑定"),
    BotCommand("forward", "查看或设置 Telegram 到 Onebot 转发"),
    BotCommand("bot_forward", "查看或设置其他 Bot 消息转发"),
    BotCommand("id_show", "查看或设置 Onebot 用户 ID 显示"),
    BotCommand("undo", "撤回所回复消息的双侧副本"),
    BotCommand("unpin", "取消所回复消息的置顶和精华"),
)


class TGBot:
    """把 PTB Application 生命周期和本项目 handlers 组合在一起。

    PTB 自带的 run_polling 会独占事件循环，不适合嵌入 FastAPI。这里手动调用
    initialize、Updater.start_polling 和 Application.start，使 Telegram Bot 与
    Uvicorn 共用当前 asyncio 事件循环。
    """

    def __init__(self, token: str, *, proxy_url: str | None = None) -> None:
        self.handlers = TGhandlers()
        self.app = self._build_app(token, proxy_url=proxy_url)
        # 避免启动和关闭并发交错，例如 shutdown 在 run 尚未完成时进入。
        self._lifecycle_lock = asyncio.Lock()

        # 三个标志分别记录真正完成的阶段。启动中途失败时只回滚已完成的部分。
        self._initialized = False
        self._polling = False
        self._running = False
        self._started_once = False
        self._commands_registered = False

    def _build_app(self, token: str, *, proxy_url: str | None = None) -> Application:
        """构建 PTB Application，并注册继承自 TGhandlers 的所有处理器。"""
        app = ApplicationBuilder()
        app.token(token)
        # 视频等媒体可能需要较长上传时间。放宽媒体写入和响应等待时间，减少
        # Telegram 已接收文件但客户端先超时所造成的结果不确定窗口。
        app.media_write_timeout(120)
        app.read_timeout(60)
        if proxy_url is not None:
            app.proxy(proxy_url)
            app.get_updates_proxy(proxy_url)

        bapp = app.build()

        bapp.add_handlers(self.handlers.get_handlers())
        return bapp

    @property
    def download_client(self):
        """暴露连接级下载客户端，同时由 handlers 实际持有引用。"""
        return self.handlers.download_client

    @download_client.setter
    def download_client(self, client) -> None:
        self.handlers.download_client = client

    async def run(self) -> None:
        """按 PTB 要求的顺序启动 Application 和 long polling。

        第一次启动会丢弃 Bot 离线期间积压的历史 Update；SnowLuma 重连时保留
        短暂掉线期间的新 Update。任一步失败都会调用 _shutdown 逆序回滚。
        """
        async with self._lifecycle_lock:
            if self._initialized:
                raise RuntimeError("TGBot is already running")

            try:
                await self.app.initialize()
                self._initialized = True

                if not self._commands_registered:
                    try:
                        await self.app.bot.set_my_commands(BOT_COMMANDS)
                        self._commands_registered = True
                    except Exception:  # noqa: BLE001
                        # 命令菜单只是可发现性增强，注册失败不能阻止桥接服务启动；
                        # 保持 False，使下一次 SnowLuma 重连时再次尝试。
                        baselog.exception("Telegram 命令列表注册失败")

                if self.app.updater is not None:
                    await self.app.updater.start_polling(
                        # 首次连接丢弃历史 Update；SnowLuma 短暂重连时保留掉线
                        # 期间积压的新消息。
                        drop_pending_updates=not self._started_once,
                    )
                    self._polling = True

                await self.app.start()
                self._running = True
                self._started_once = True
            except BaseException as error:
                # BaseException 包含任务取消；启动任务被取消时同样必须释放 PTB 资源。
                try:
                    await self._shutdown()
                except BaseException as shutdown_error:  # noqa: BLE001
                    error.add_note(f"TGBot cleanup failed: {shutdown_error!r}")
                raise

    async def shutdown(self) -> None:
        """停止更新处理并释放 PTB 的 Bot API 客户端。"""
        async with self._lifecycle_lock:
            await self._shutdown()

    async def stop(self) -> None:
        """停止产生 Telegram 消息，但保留 Bot API 客户端供队列排空。"""
        async with self._lifecycle_lock:
            await self._stop()

    async def _shutdown(self) -> None:
        """按照启动的相反顺序关闭实际处于活动状态的组件。"""
        await self._stop()

        if self._initialized:
            # 消息消费者排空后才关闭 Bot API 请求客户端。
            await self.app.shutdown()
            self._initialized = False

    async def _stop(self) -> None:
        """停止 polling 和更新任务，但保持 Application 已初始化。"""
        if self._polling and self.app.updater is not None:
            # 先停止获取新 Update，防止关闭过程中 handlers 继续生产消息。
            await self.app.updater.stop()
            self._polling = False

        if self._running:
            # 等待 PTB 当前跟踪的更新处理任务结束。
            await self.app.stop()
            self._running = False
