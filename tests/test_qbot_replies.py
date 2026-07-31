import unittest
from functools import partial
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.messages import OneBotMessage, SendTarget, SendTask
from src.qbot import q_gateway, receive_onebot_event


class OneBotReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = MessageBus()
        self.bot = cast(ExtBot[None], SimpleNamespace())
        self.client = cast(httpx.AsyncClient, SimpleNamespace())

    async def _receive(self, event: dict) -> OneBotMessage:
        with patch("src.qbot.message_bus", self.bus):
            await receive_onebot_event(event, self.bot, self.client)
        task = await self.bus.telegram_queue.get()
        try:
            self.assertIsInstance(task, SendTask)
            assert isinstance(task, SendTask)
            self.assertIs(task.target, SendTarget.TELEGRAM)
            self.assertIsInstance(task.send, partial)
            assert isinstance(task.send, partial)
            message = task.send.args[0]
            self.assertIsInstance(message, OneBotMessage)
            assert isinstance(message, OneBotMessage)
            return message
        finally:
            self.bus.telegram_queue.task_done()

    async def test_reply_segment_is_parsed_into_message(self) -> None:
        message = await self._receive(
            {
                "post_type": "message",
                "message_type": "group",
                "self_id": 1,
                "user_id": 2,
                "group_id": 3,
                "message_id": 4,
                "sender": {"nickname": "User"},
                "message": [
                    {"type": "reply", "data": {"id": "123"}},
                    {"type": "text", "data": {"text": "reply"}},
                ],
            }
        )
        self.assertEqual(message.reply_message_id, 123)

    async def test_group_card_is_preferred_over_nickname(self) -> None:
        message = await self._receive(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "self_id": 1,
                "user_id": 2,
                "group_id": 3,
                "message_id": 4,
                "sender": {"nickname": "Nickname", "card": "Group Card"},
                "message": [{"type": "text", "data": {"text": "message"}}],
            }
        )
        self.assertEqual(message.sender_name, "Group Card")

    async def test_anonymous_name_is_used_instead_of_sender(self) -> None:
        message = await self._receive(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "anonymous",
                "self_id": 1,
                "user_id": 2,
                "group_id": 3,
                "message_id": 4,
                "anonymous": {"id": 5, "name": "Anonymous", "flag": "flag"},
                "sender": {"nickname": "Unreliable", "card": "Unreliable Card"},
                "message": [{"type": "text", "data": {"text": "message"}}],
            }
        )
        self.assertEqual(message.sender_name, "Anonymous")
        self.assertFalse(message.sender_name_is_fallback)

    async def test_missing_sender_name_is_marked_as_id_fallback(self) -> None:
        message = await self._receive(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "self_id": 1,
                "user_id": 234,
                "group_id": 3,
                "message_id": 4,
                "sender": {"nickname": "", "card": ""},
                "message": [{"type": "text", "data": {"text": "message"}}],
            }
        )
        self.assertEqual(message.sender_name, "OneBot 用户 234")
        self.assertTrue(message.sender_name_is_fallback)

    async def test_notice_subtype_is_not_queued(self) -> None:
        with patch("src.qbot.message_bus", self.bus):
            await receive_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "notice",
                },
                self.bot,
                self.client,
            )
        self.assertTrue(self.bus.telegram_queue.empty())

    async def test_null_segment_data_is_normalized(self) -> None:
        message = await self._receive(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "self_id": 1,
                "user_id": 2,
                "group_id": 3,
                "message_id": 4,
                "message": [{"type": "rps", "data": None}],
            }
        )
        self.assertEqual(message.message, [{"type": "rps", "data": {}}])

    async def test_malformed_segment_is_rejected(self) -> None:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.qlog.warning") as warning,
        ):
            await receive_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "normal",
                    "self_id": 1,
                    "user_id": 2,
                    "group_id": 3,
                    "message_id": 4,
                    "message": [{"type": "text", "data": "not-an-object"}],
                },
                self.bot,
                self.client,
            )
        self.assertTrue(self.bus.telegram_queue.empty())
        warning.assert_called_once()

    async def test_full_telegram_queue_does_not_block_onebot_reader(self) -> None:
        bus = MessageBus(maxsize=1)
        await bus.put(
            SendTask(target=SendTarget.TELEGRAM, send=AsyncMock())
        )
        with (
            patch("src.qbot.message_bus", bus),
            patch("src.qbot.qlog.error") as error,
            patch("src.qbot.enqueue_onebot_notice") as notice,
        ):
            await receive_onebot_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 1,
                    "user_id": 2,
                    "group_id": 3,
                    "message_id": 4,
                    "message": [{"type": "text", "data": {"text": "full"}}],
                },
                self.bot,
                self.client,
            )
        self.assertEqual(bus.telegram_queue.qsize(), 1)
        error.assert_called_once()
        notice.assert_called_once_with(
            q_gateway,
            q_group_id=3,
            text="消息发送到 Telegram 失败：发送队列已满，请稍后重试。",
        )


if __name__ == "__main__":
    unittest.main()
