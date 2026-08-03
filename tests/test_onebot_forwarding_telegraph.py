import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.constants import ParseMode
from telegram.ext import ExtBot

from src.forwarding import forward_onebot_to_telegram
from src.messages import OneBotMessage, TelegraphPageRef
from src.qbot import QGateway


class OneBotForwardingTelegraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_segment_sends_page_url_and_reuses_it(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    RuntimeError("temporary telegram failure"),
                    SimpleNamespace(message_id=31),
                ]
            )
        )
        gateway = cast(QGateway, SimpleNamespace())
        message = OneBotMessage(
            message_id=456,
            group_id=123,
            user_id=789,
            sender_name="测试发送者",
            message=[{"type": "forward", "data": {"id": "forward-test-id"}}],
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-100123),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(),
        )
        page = TelegraphPageRef(
            title="群聊的聊天记录 - 0123456789abcdef",
            url="https://telegra.ph/test-page",
        )
        create_page = AsyncMock(return_value=page)

        with (
            patch("src.forwarding.sql", database),
            patch("src.onebot_forward.create_forward_page", create_page),
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary telegram failure"):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    cast(httpx.AsyncClient, SimpleNamespace()),
                    gateway,
                )
            await forward_onebot_to_telegram(
                message,
                cast(ExtBot[None], bot),
                cast(httpx.AsyncClient, SimpleNamespace()),
                gateway,
            )

        create_page.assert_awaited_once()
        self.assertEqual(message.telegraph_pages["forward-test-id"], page)
        expected_text = (
            "测试发送者:\n"
            "[群聊的聊天记录 \\- 0123456789abcdef](https://telegra.ph/test-page)"
        )
        self.assertEqual(bot.send_message.await_args_list[0].kwargs["text"], expected_text)
        self.assertEqual(bot.send_message.await_args_list[1].kwargs["text"], expected_text)
        self.assertIs(
            bot.send_message.await_args_list[0].kwargs["parse_mode"],
            ParseMode.MARKDOWN_V2,
        )
        database.set_message_mapping.assert_awaited_once()
