import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram import LinkPreviewOptions
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.messages import SendLane, SendTarget, SendTask
from src.qbot import QGateway, receive_onebot_event
from src.sql import Sql


class OneBotPokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "poke.sqlite3")
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
                {"card": "测试用户", "nickname": "User"},
                {"card": "测试机器人", "nickname": "Bot"},
            ]
        )
        self.gateway = cast(
            QGateway,
            SimpleNamespace(get_group_member_info=self.get_group_member_info),
        )

    async def asyncTearDown(self) -> None:
        await self.sql.close()
        self.directory.cleanup()

    async def _send_poke(self, event: dict) -> SendTask:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.q_gateway", self.gateway),
        ):
            await receive_onebot_event(event, self.bot, self.client)
        task = await self.bus.telegram_event_queue.get()
        self.bus.telegram_event_queue.task_done()
        self.assertIsInstance(task, SendTask)
        assert isinstance(task, SendTask)
        self.assertIs(task.target, SendTarget.TELEGRAM)
        self.assertIs(task.lane, SendLane.EVENT)
        return task

    async def test_poke_sends_names_and_ids(self) -> None:
        task = await self._send_poke(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 123,
                "user_id": 101,
                "target_id": 102,
                "action": "揉了揉",
                "suffix": "的脸，怎么了？",
                "action_img_url": "http://example.test/expression.jpg",
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text=(
                "测试用户[101] 揉了揉 "
                "测试机器人[102] 的脸，怎么了？"
            ),
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self.get_group_member_info.assert_any_await(123, 101)
        self.get_group_member_info.assert_any_await(123, 102)

    async def test_poke_hides_ids_when_disabled(self) -> None:
        await self.sql.set_id_show_enabled(-100123, False)
        task = await self._send_poke(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 123,
                "user_id": 101,
                "target_id": 102,
                "action": "揉了揉",
                "suffix": "的脸，怎么了？",
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="测试用户 揉了揉 测试机器人 的脸，怎么了？",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def test_poke_self_uses_self_as_target(self) -> None:
        task = await self._send_poke(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 123,
                "user_id": 101,
                "target_id": 101,
                "action": "戳了戳",
                "suffix": "的脸",
            }
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="测试用户[101] 戳了戳 自己 的脸",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        self.get_group_member_info.assert_awaited_once_with(123, 101)

    async def test_other_notify_subtype_is_ignored(self) -> None:
        with patch("src.qbot.message_bus", self.bus):
            await receive_onebot_event(
                {
                    "post_type": "notice",
                    "notice_type": "notify",
                    "sub_type": "honor",
                    "group_id": 123,
                },
                self.bot,
                self.client,
            )
        self.assertTrue(self.bus.telegram_event_queue.empty())

    async def test_malformed_poke_is_not_queued(self) -> None:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.qlog.warning") as warning,
        ):
            await receive_onebot_event(
                {
                    "post_type": "notice",
                    "notice_type": "notify",
                    "sub_type": "poke",
                    "group_id": 123,
                    "user_id": 101,
                    "target_id": 102,
                    "action": "",
                    "suffix": "suffix",
                },
                self.bot,
                self.client,
            )
        self.assertTrue(self.bus.telegram_event_queue.empty())
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
