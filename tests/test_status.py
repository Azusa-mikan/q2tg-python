import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import CommandHandler

from src.runtime_stats import ConversionAverages, QueueSizes, RuntimeInfo
from src.tgbot.handlers import TGhandlers


class StatusHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_replies_with_runtime_info(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        info = RuntimeInfo(
            rss="12.34 MiB",
            queues=QueueSizes(
                onebot=1,
                telegram=2,
                retry=3,
                media_processing=4,
            ),
            conversion_averages=ConversionAverages(
                voice=None,
                video=1.234,
                sticker_static=0.5,
                sticker_tgs=None,
                sticker_video=2.0,
            ),
        )

        with patch("src.tgbot.handlers.get_runtime_info", return_value=info):
            await TGhandlers().get_status(
                update,  # pyright: ignore[reportArgumentType]
                SimpleNamespace(),  # pyright: ignore[reportArgumentType]
            )

        message.reply_text.assert_awaited_once_with(
            "Q2TG 状态\n\n"
            "RSS：12.34 MiB\n\n"
            "消息队列\n"
            "OneBot：1\n"
            "Telegram：2\n"
            "重试：3\n"
            "媒体处理：4\n\n"
            "平均转换耗时（最近 30 次）\n"
            "语音：暂无数据\n"
            "视频：1.23 秒\n"
            "静态贴纸：0.50 秒\n"
            "TGS 贴纸：暂无数据\n"
            "视频贴纸：2.00 秒"
        )

    def test_status_command_is_registered(self) -> None:
        commands = [
            handler.commands
            for handler in TGhandlers().get_handlers()
            if isinstance(handler, CommandHandler)
        ]

        self.assertIn(frozenset({"status"}), commands)
