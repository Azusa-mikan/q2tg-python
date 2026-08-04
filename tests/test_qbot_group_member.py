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
from src.messages import SendLane, SendTarget, SendTask
from src.qbot import QGateway, receive_onebot_event
from src.sql import Sql


class TestOneBotGroupMember:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_database(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "group-member.sqlite3")
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
            return_value={"card": "用户名", "nickname": "User"}
        )
        self.gateway = cast(
            QGateway,
            SimpleNamespace(get_group_member_info=self.get_group_member_info),
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
    async def test_group_increase_sends_join_message_and_ignores_sub_type(self) -> None:
        task = await self._send_event(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "unexpected",
                "group_id": 123,
                "operator_id": 103,
                "user_id": 101,
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="用户名[101] 加入群聊",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self.get_group_member_info.assert_awaited_once_with(
            123,
            101,
            no_cache=True,
        )

    @pytest.mark.asyncio
    async def test_group_decrease_sends_leave_message_without_operator(self) -> None:
        await self.sql.set_id_show_enabled(-100123, False)
        task = await self._send_event(
            {
                "post_type": "notice",
                "notice_type": "group_decrease",
                "sub_type": "kick_me",
                "group_id": 123,
                "operator_id": 103,
                "user_id": 101,
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="用户名 退出群聊",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self.get_group_member_info.assert_awaited_once_with(123, 101)

    @pytest.mark.asyncio
    async def test_missing_name_follows_id_show_fallback(self) -> None:
        self.get_group_member_info.side_effect = RuntimeError("member left")
        task = await self._send_event(
            {
                "post_type": "notice",
                "notice_type": "group_decrease",
                "group_id": 123,
                "user_id": 101,
            }
        )

        with (
            patch("src.forwarding.sql", self.sql),
            patch("src.forwarding.baselog.warning"),
        ):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="101 退出群聊",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @pytest.mark.asyncio
    async def test_malformed_member_event_is_not_queued(self) -> None:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.qlog.warning") as warning,
        ):
            await receive_onebot_event(
                {
                    "post_type": "notice",
                    "notice_type": "group_increase",
                    "group_id": 123,
                    "user_id": "101",
                },
                self.bot,
                self.client,
            )

        assert self.bus.telegram_event_queue.empty()
        warning.assert_called_once()
