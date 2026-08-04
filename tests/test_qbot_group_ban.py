from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from telegram import LinkPreviewOptions
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.forwarding import format_duration
from src.messages import SendLane, SendTarget, SendTask
from src.qbot import QGateway, receive_onebot_event
from src.sql import Sql


class TestOneBotGroupBan:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_database(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "group-ban.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -100123)
        self.bus = MessageBus()
        self.send_message = AsyncMock()
        self.bot = cast(
            ExtBot[None],
            SimpleNamespace(send_message=self.send_message),
        )
        self.client = cast(httpx.AsyncClient, SimpleNamespace())
        self.get_group_member_info = AsyncMock(
            side_effect=[
                {"card": "用户名", "nickname": "User"},
                {"card": "测试管理员", "nickname": "Operator"},
            ]
        )
        self.gateway = cast(
            QGateway,
            SimpleNamespace(
                get_group_member_info=self.get_group_member_info,
            ),
        )

        try:
            yield
        finally:
            await self.sql.close()
            self.directory.cleanup()

    async def _send_event(self, event: dict) -> SendTask:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.q_gateway", self.gateway),
        ):
            await receive_onebot_event(event, self.bot, self.client)
        task = await self.bus.telegram_event_queue.get()
        self.bus.telegram_event_queue.task_done()
        assert isinstance(task, SendTask)
        assert task.target is SendTarget.TELEGRAM
        assert task.lane is SendLane.EVENT
        return task

    @pytest.mark.asyncio
    async def test_ban_sends_scaled_duration_with_ids_enabled(self) -> None:
        task = await self._send_event(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 123,
                "operator_id": 103,
                "user_id": 101,
                "duration": 600,
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="用户名[101] 被管理员 测试管理员[103] 禁言 10 分钟",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        assert self.get_group_member_info.await_count == 2

    @pytest.mark.asyncio
    async def test_lift_ban_hides_ids_when_disabled(self) -> None:
        await self.sql.set_id_show_enabled(-100123, False)
        task = await self._send_event(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "sub_type": "lift_ban",
                "group_id": 123,
                "operator_id": 103,
                "user_id": 101,
                "duration": 0,
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="用户名 被管理员 测试管理员 解除禁言",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @pytest.mark.asyncio
    async def test_malformed_ban_is_not_queued(self) -> None:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.qlog.warning") as warning,
        ):
            await receive_onebot_event(
                {
                    "post_type": "notice",
                    "notice_type": "group_ban",
                    "sub_type": "ban",
                    "group_id": 123,
                    "operator_id": 103,
                    "user_id": 101,
                    "duration": -1,
                },
                self.bot,
                self.client,
            )

        assert self.bus.telegram_event_queue.empty()
        warning.assert_called_once()

    @pytest.mark.parametrize(
        ("duration", "expected"),
        [
            (0, "0 秒"),
            (59, "59 秒"),
            (600, "10 分钟"),
            (90_061, "1 天 1 小时 1 分钟 1 秒"),
        ],
    )
    def test_duration_scaling(self, duration: int, expected: str) -> None:
        assert format_duration(duration) == expected
