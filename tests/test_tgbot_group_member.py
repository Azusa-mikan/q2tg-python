import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from telegram import Update
from telegram.ext import ContextTypes

from src.bus import MessageBus
from src.messages import SendLane, SendTarget, SendTask
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


class TelegramGroupMemberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "telegram-group-member.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -100_123)
        self.bus = MessageBus()
        self.send_group_message = AsyncMock(return_value=301)
        self.gateway = SimpleNamespace(send_group_message=self.send_group_message)
        self.handler = TGhandlers()
        self.context = cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace())

    async def asyncTearDown(self) -> None:
        await self.sql.close()
        self.directory.cleanup()

    async def _receive(self, message: SimpleNamespace) -> SendTask:
        update = cast(Update, SimpleNamespace(effective_message=message))
        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.message_bus", self.bus),
            patch("src.tgbot.handlers.q_gateway", self.gateway),
        ):
            await self.handler.receive_group_member(update, self.context)
        task = await self.bus.onebot_event_queue.get()
        self.bus.onebot_event_queue.task_done()
        self.assertIsInstance(task, SendTask)
        assert isinstance(task, SendTask)
        self.assertIs(task.target, SendTarget.ONEBOT)
        self.assertIs(task.lane, SendLane.EVENT)
        return task

    async def test_new_members_are_sent_in_one_message(self) -> None:
        task = await self._receive(
            SimpleNamespace(
                chat_id=-100_123,
                new_chat_members=(
                    SimpleNamespace(full_name="测试用户甲"),
                    SimpleNamespace(full_name="测试用户乙"),
                ),
                left_chat_member=None,
            )
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_group_message.assert_awaited_once_with(
            group_id=123,
            message=[
                {
                    "type": "text",
                    "data": {"text": "测试用户甲 加入了群聊\n测试用户乙 加入了群聊"},
                }
            ],
        )

    async def test_left_member_is_sent_to_onebot(self) -> None:
        task = await self._receive(
            SimpleNamespace(
                chat_id=-100_123,
                new_chat_members=(),
                left_chat_member=SimpleNamespace(full_name="测试用户丙"),
            )
        )

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_group_message.assert_awaited_once_with(
            group_id=123,
            message=[
                {"type": "text", "data": {"text": "测试用户丙 退出了群聊"}}
            ],
        )

    async def test_disabled_forwarding_does_not_queue_event(self) -> None:
        await self.sql.set_tg_forward_enabled(-100_123, False)
        update = cast(
            Update,
            SimpleNamespace(
                effective_message=SimpleNamespace(
                    chat_id=-100_123,
                    new_chat_members=(SimpleNamespace(full_name="测试用户丁"),),
                    left_chat_member=None,
                )
            ),
        )

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.message_bus", self.bus),
        ):
            await self.handler.receive_group_member(update, self.context)

        self.assertTrue(self.bus.onebot_event_queue.empty())

    async def test_event_is_dropped_if_forwarding_is_disabled_while_queued(self) -> None:
        task = await self._receive(
            SimpleNamespace(
                chat_id=-100_123,
                new_chat_members=(SimpleNamespace(full_name="测试用户戊"),),
                left_chat_member=None,
            )
        )
        await self.sql.set_tg_forward_enabled(-100_123, False)

        with patch("src.forwarding.sql", self.sql):
            await task.send()

        self.send_group_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
