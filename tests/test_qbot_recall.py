import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.messages import SendLane, SendTarget, SendTask
from src.qbot import receive_onebot_event
from src.sql import Sql


class OneBotRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "recall.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -100123)
        self.bus = MessageBus()
        self.delete_messages = AsyncMock()
        self.bot = cast(
            ExtBot[None],
            SimpleNamespace(delete_messages=self.delete_messages),
        )
        self.client = cast(httpx.AsyncClient, SimpleNamespace())

    async def asyncTearDown(self) -> None:
        await self.sql.close()
        self.directory.cleanup()

    async def _receive_recall(self, event: dict) -> None:
        with patch("src.qbot.message_bus", self.bus):
            await receive_onebot_event(event, self.bot, self.client)

    async def test_group_recall_deletes_all_mapped_telegram_messages(self) -> None:
        message_id = -1001
        await self.sql.set_message_mapping(
            q_group_id=123,
            q_message_ids=(message_id,),
            tg_chat_id=-100123,
            tg_message_ids=tuple(range(1, 102)),
            q_user_id=101,
        )
        await self._receive_recall(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": 123,
                "message_id": message_id,
            }
        )

        task = await self.bus.telegram_event_queue.get()
        try:
            self.assertIsInstance(task, SendTask)
            assert isinstance(task, SendTask)
            self.assertIs(task.target, SendTarget.TELEGRAM)
            self.assertEqual(task.max_attempts, 3)
            with patch("src.forwarding.sql", self.sql):
                await task.send()
        finally:
            self.bus.telegram_event_queue.task_done()

        self.assertEqual(self.delete_messages.await_count, 2)
        self.delete_messages.assert_any_await(
            chat_id=-100123,
            message_ids=tuple(range(1, 101)),
        )
        self.delete_messages.assert_any_await(
            chat_id=-100123,
            message_ids=(101,),
        )

    async def test_group_recall_without_mapping_does_nothing(self) -> None:
        await self._receive_recall(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": 123,
                "message_id": 999,
            }
        )
        task = await self.bus.telegram_event_queue.get()
        try:
            assert isinstance(task, SendTask)
            with (
                patch("src.forwarding.sql", self.sql),
                patch("src.forwarding.baselog.warning") as warning,
            ):
                await task.send()
        finally:
            self.bus.telegram_event_queue.task_done()

        self.delete_messages.assert_not_awaited()
        warning.assert_called_once()

    async def test_malformed_group_recall_is_not_queued(self) -> None:
        with patch("src.qbot.qlog.warning") as warning:
            await self._receive_recall(
                {
                    "post_type": "notice",
                    "notice_type": "group_recall",
                    "group_id": 123,
                    "message_id": "1001",
                }
            )

        self.assertTrue(self.bus.telegram_event_queue.empty())
        warning.assert_called_once()

    async def test_other_notice_is_ignored(self) -> None:
        await self._receive_recall(
            {
                "post_type": "notice",
                "notice_type": "unknown_notice",
                "group_id": 123,
            }
        )
        self.assertTrue(self.bus.telegram_event_queue.empty())

    async def test_recall_waits_for_inflight_negative_message_id(self) -> None:
        message_id = -1001
        send_started = asyncio.Event()
        allow_send = asyncio.Event()

        async def send_message(**kwargs):
            send_started.set()
            await allow_send.wait()
            return SimpleNamespace(message_id=88)

        self.bot.send_message = AsyncMock(side_effect=send_message)
        message_consumer = asyncio.create_task(
            self.bus.consume(SendTarget.TELEGRAM, SendLane.MESSAGE)
        )
        event_consumer = asyncio.create_task(
            self.bus.consume(SendTarget.TELEGRAM, SendLane.EVENT)
        )
        try:
            with (
                patch("src.qbot.message_bus", self.bus),
                patch("src.bus.message_bus", self.bus),
                patch("src.forwarding.sql", self.sql),
            ):
                await receive_onebot_event(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "sub_type": "normal",
                        "self_id": 102,
                        "user_id": 101,
                        "group_id": 123,
                        "message_id": message_id,
                        "sender": {"nickname": "User", "card": ""},
                        "message": [{"type": "text", "data": {"text": "123"}}],
                    },
                    self.bot,
                    self.client,
                )
                await send_started.wait()
                await receive_onebot_event(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": message_id,
                    },
                    self.bot,
                    self.client,
                )
                self.assertTrue(self.bus.telegram_event_queue.empty())
                allow_send.set()
                await self.bus.join(SendTarget.TELEGRAM, SendLane.MESSAGE)
                await self.bus.join(SendTarget.TELEGRAM, SendLane.EVENT)
        finally:
            allow_send.set()
            message_consumer.cancel()
            event_consumer.cancel()
            await asyncio.gather(
                message_consumer,
                event_consumer,
                return_exceptions=True,
            )

        mapping = await self.sql.get_tg_message(123, message_id)
        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(mapping.tg_message_ids, (88,))
        self.bot.send_message.assert_awaited_once_with(
            chat_id=-100123,
            text="User:\n123",
            reply_parameters=None,
        )
        self.delete_messages.assert_awaited_once_with(
            chat_id=-100123,
            message_ids=(88,),
        )


if __name__ == "__main__":
    unittest.main()
