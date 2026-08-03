import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import ExtBot

from src.forwarding import forward_onebot_to_telegram, onebot_message_text
from src.messages import OneBotMessage
from src.qbot import QGateway


class MentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_preserve_segment_order_and_reuse_member_lookup(self) -> None:
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(
                side_effect=[
                    {"card": "Group Card", "nickname": "Nickname"},
                    {"card": "", "nickname": "Second User"},
                ]
            )
        )

        text = await onebot_message_text(
            [
                {"type": "text", "data": {"text": "hello "}},
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": " and "}},
                {"type": "at", "data": {"qq": "200"}},
                {"type": "text", "data": {"text": " / "}},
                {"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": " / "}},
                {"type": "at", "data": {"qq": "all"}},
            ],
            123,
            cast(QGateway, gateway),
        )

        self.assertEqual(
            text,
            "hello @Group Card and @Second User / @Group Card / @全体成员",
        )
        self.assertEqual(gateway.get_group_member_info.await_count, 2)
        gateway.get_group_member_info.assert_any_await(123, 100)
        gateway.get_group_member_info.assert_any_await(123, 200)

    async def test_different_member_lookups_run_concurrently(self) -> None:
        both_started = asyncio.Event()
        release = asyncio.Event()
        started = 0

        async def lookup(group_id: int, user_id: int) -> dict[str, str]:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await release.wait()
            return {"card": f"User {user_id}", "nickname": ""}

        gateway = SimpleNamespace(get_group_member_info=AsyncMock(side_effect=lookup))
        task = asyncio.create_task(
            onebot_message_text(
                [
                    {"type": "at", "data": {"qq": "100"}},
                    {"type": "at", "data": {"qq": "200"}},
                ],
                123,
                cast(QGateway, gateway),
            )
        )
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            self.assertEqual(await task, "@User 100@User 200")
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_member_names_are_reused_across_calls(self) -> None:
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(
                return_value={"card": "Group Card", "nickname": ""}
            )
        )
        member_names: dict[int, str | None] = {}
        message = [{"type": "at", "data": {"qq": "100"}}]

        first = await onebot_message_text(
            message,
            123,
            cast(QGateway, gateway),
            member_names=member_names,
        )
        second = await onebot_message_text(
            message,
            123,
            cast(QGateway, gateway),
            id_show_enabled=True,
            member_names=member_names,
        )

        self.assertEqual(first, "@Group Card")
        self.assertEqual(second, "@Group Card[100]")
        gateway.get_group_member_info.assert_awaited_once_with(123, 100)

    async def test_member_lookup_failure_falls_back_to_qq_number(self) -> None:
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(side_effect=RuntimeError("failed"))
        )

        with patch("src.forwarding.baselog.warning") as warning:
            text = await onebot_message_text(
                [{"type": "at", "data": {"qq": "102"}}],
                123,
                cast(QGateway, gateway),
            )

        self.assertEqual(text, "@OneBot 用户")
        warning.assert_called_once()

    async def test_id_show_appends_qq_number_to_mention(self) -> None:
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(
                return_value={"card": "测试用户", "nickname": "Nickname"}
            )
        )

        text = await onebot_message_text(
            [{"type": "at", "data": {"qq": "101"}}],
            123,
            cast(QGateway, gateway),
            id_show_enabled=True,
        )

        self.assertEqual(text, "@测试用户[101]")

    async def test_id_show_uses_qq_number_as_fallback(self) -> None:
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(side_effect=RuntimeError("failed"))
        )

        with patch("src.forwarding.baselog.warning"):
            text = await onebot_message_text(
                [{"type": "at", "data": {"qq": "101"}}],
                123,
                cast(QGateway, gateway),
                id_show_enabled=True,
            )

        self.assertEqual(text, "@101")

    async def test_onebot_mention_is_visible_in_telegram_message(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=201))
        )
        gateway = SimpleNamespace(
            get_group_member_info=AsyncMock(
                return_value={"card": "", "nickname": "Bot Name"}
            )
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=1,
            sender_name="OneBot User",
            message=[
                {"type": "at", "data": {"qq": "102"}},
                {"type": "text", "data": {"text": " "}},
            ],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch(
                    "src.forwarding.sql.get_tg_message",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                patch("src.forwarding.sql.get_tg_group", new_callable=AsyncMock, return_value=-456),
                patch(
                    "src.forwarding.sql.get_id_show_enabled",
                    new_callable=AsyncMock,
                    return_value=False,
                ),
                patch("src.forwarding.sql.set_message_mapping", new_callable=AsyncMock),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                    cast(QGateway, gateway),
                )

        self.assertEqual(bot.send_message.await_args.kwargs["text"], "OneBot User:\n@Bot Name ")


if __name__ == "__main__":
    unittest.main()
