import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import ExtBot

from src.forwarding import forward_onebot_to_telegram, forward_telegram_to_onebot
from src.messages import OneBotMessage, TelegramMessage
from src.qbot import QGateway
from src.sql import Sql


class ReplyForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.cache = Sql(Path(self.directory.name) / "cache.sqlite3")
        await self.cache.load()
        await self.cache.bind_group(123, -456)
        await self.cache.set_message_mapping(
            q_group_id=123,
            q_message_ids=(99, 100),
            tg_chat_id=-456,
            tg_message_ids=(200, 201),
        )

    async def asyncTearDown(self) -> None:
        await self.cache.close()
        self.directory.cleanup()

    async def test_onebot_reply_uses_first_telegram_message(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=202)))
        async with httpx.AsyncClient() as client:
            with patch("src.forwarding.sql", self.cache):
                await forward_onebot_to_telegram(
                    OneBotMessage(
                        message_id=101,
                        group_id=123,
                        user_id=1,
                        sender_name="OneBot User",
                        message=[{"type": "text", "data": {"text": "reply"}}],
                        reply_message_id=100,
                    ),
                    cast(ExtBot[None], bot),
                    client,
                )

        reply_parameters = bot.send_message.await_args.kwargs["reply_parameters"]
        self.assertEqual(reply_parameters.message_id, 200)

    async def test_telegram_reply_adds_onebot_reply_segment(self) -> None:
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=102))
        with patch("src.forwarding.sql", self.cache):
            await forward_telegram_to_onebot(
                TelegramMessage(
                    message_ids=(202,),
                    group_id=-456,
                    user_id=2,
                    sender_name="Telegram User",
                    text="reply",
                    reply_message_id=201,
                ),
                cast(QGateway, gateway),
            )

        segments = gateway.send_group_message.await_args.kwargs["message"]
        self.assertEqual(segments[0], {"type": "reply", "data": {"id": "100"}})

    async def test_missing_mapping_falls_back_to_normal_message(self) -> None:
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=103))
        with patch("src.forwarding.sql", self.cache):
            await forward_telegram_to_onebot(
                TelegramMessage(
                    message_ids=(203,),
                    group_id=-456,
                    user_id=2,
                    sender_name="Telegram User",
                    text="reply",
                    reply_message_id=999,
                ),
                cast(QGateway, gateway),
            )

        segments = gateway.send_group_message.await_args.kwargs["message"]
        self.assertNotEqual(segments[0]["type"], "reply")


if __name__ == "__main__":
    unittest.main()
