from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.ext import CommandHandler

from src.runtime_stats import ConversionAverages, QueueSizes, RuntimeInfo
from src.tgbot.handlers import TGhandlers


class TestStatusHandler:
    @pytest.mark.asyncio
    async def test_status_replies_with_runtime_info(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        info = RuntimeInfo(
            rss="12.34 MiB",
            queues=QueueSizes(
                onebot_messages=1,
                onebot_events=2,
                onebot_system=3,
                telegram_messages=4,
                telegram_events=5,
                telegram_system=6,
                retry=7,
                media_processing=8,
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
            "OneBot 消息：1\n"
            "OneBot 事件：2\n"
            "OneBot 系统通知：3\n"
            "Telegram 消息：4\n"
            "Telegram 事件：5\n"
            "Telegram 系统通知：6\n"
            "重试：7\n"
            "媒体处理：8\n\n"
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

        assert frozenset({"status"}) in commands
