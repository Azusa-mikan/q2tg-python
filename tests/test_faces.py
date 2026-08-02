import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ExtBot

from src.face import (
    _STICKER_PACK_CACHE,
    normalize_onebot_face_message,
    onebot_super_face_file_id,
    render_onebot_face,
)
from src.forwarding import forward_onebot_to_telegram, onebot_message_text
from src.messages import OneBotMessage


class FaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _STICKER_PACK_CACHE.clear()

    async def test_face_preserves_segment_order(self) -> None:
        text = await onebot_message_text(
            [
                {"type": "text", "data": {"text": "测试文本 "}},
                {"type": "face", "data": {"id": "176"}},
                {"type": "text", "data": {"text": " 测试文本"}},
            ],
            123,
            markdown_v2=True,
        )

        self.assertEqual(
            text,
            r"测试文本 [\[/小纠结\]](https://t.me/qq_face/89) 测试文本",
        )

    async def test_face_without_channel_mapping_falls_back_to_label(self) -> None:
        self.assertEqual(render_onebot_face("999999"), r"\[表情:999999\]")

    async def test_generated_face_name_text_is_removed_for_all_faces(self) -> None:
        self.assertEqual(
            normalize_onebot_face_message(
                [
                    {"type": "face", "data": {"id": "421"}},
                    {"type": "text", "data": {"text": "[大火车]"}},
                ]
            ),
            [{"type": "face", "data": {"id": "421"}}],
        )

    async def test_generated_face_name_normalization_keeps_real_mixed_text(self) -> None:
        message = [
            {"type": "face", "data": {"id": "429"}},
            {"type": "text", "data": {"text": "测试"}},
        ]
        self.assertIs(normalize_onebot_face_message(message), message)

    async def test_unmapped_super_face_keeps_hyperlink_without_duplicate_text(
        self,
    ) -> None:
        text = await onebot_message_text(
            normalize_onebot_face_message(
                [
                    {"type": "face", "data": {"id": "421"}},
                    {"type": "text", "data": {"text": "[大火车]"}},
                ]
            ),
            123,
            markdown_v2=True,
        )
        self.assertEqual(
            text,
            r"[\[/大火车\]](https://t.me/qq_face/411)",
        )

    async def test_super_face_sticker_pack_is_cached(self) -> None:
        stickers = [SimpleNamespace(file_id=f"sticker-{index}") for index in range(9)]
        bot = SimpleNamespace(
            get_sticker_set=AsyncMock(return_value=SimpleNamespace(stickers=stickers))
        )

        first = await onebot_super_face_file_id(cast(Bot, bot), "429")
        second = await onebot_super_face_file_id(cast(Bot, bot), "432")

        self.assertEqual(first, "sticker-0")
        self.assertEqual(second, "sticker-8")
        bot.get_sticker_set.assert_awaited_once_with("qq_snake")

    async def test_face_message_is_sent_as_markdown_v2(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=201))
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=456,
            sender_name="Sender_Name",
            message=[
                {"type": "text", "data": {"text": "测试_文本 "}},
                {"type": "face", "data": {"id": "176"}},
            ],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch(
                    "src.forwarding.sql.get_tg_message",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                patch(
                    "src.forwarding.sql.get_tg_group",
                    new_callable=AsyncMock,
                    return_value=-789,
                ),
                patch(
                    "src.forwarding.sql.set_message_mapping",
                    new_callable=AsyncMock,
                ),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        self.assertEqual(
            bot.send_message.await_args.kwargs["text"],
            "Sender\\_Name:\n测试\\_文本 "
            r"[\[/小纠结\]](https://t.me/qq_face/89)",
        )
        self.assertEqual(
            bot.send_message.await_args.kwargs["parse_mode"],
            ParseMode.MARKDOWN_V2,
        )

    async def test_single_super_face_is_sent_as_sticker(self) -> None:
        stickers = [SimpleNamespace(file_id=f"sticker-{index}") for index in range(9)]
        bot = SimpleNamespace(
            get_sticker_set=AsyncMock(
                return_value=SimpleNamespace(stickers=stickers)
            ),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=201)),
            send_sticker=AsyncMock(return_value=SimpleNamespace(message_id=202)),
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=456,
            sender_name="OneBot User",
            message=[
                {"type": "face", "data": {"id": "429"}},
                {"type": "text", "data": {"text": "[蛇年快乐]"}},
            ],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch(
                    "src.forwarding.sql.get_tg_message",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                patch(
                    "src.forwarding.sql.get_tg_group",
                    new_callable=AsyncMock,
                    return_value=-789,
                ),
                patch(
                    "src.forwarding.sql.set_message_mapping",
                    new_callable=AsyncMock,
                ) as set_mapping,
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_awaited_once_with(
            chat_id=-789,
            text="OneBot User:",
            reply_parameters=None,
        )
        bot.send_sticker.assert_awaited_once_with(
            chat_id=-789,
            sticker="sticker-0",
        )
        self.assertEqual(message.tg_message_ids, [201, 202])
        assert set_mapping.await_args is not None
        self.assertEqual(set_mapping.await_args.kwargs["tg_message_ids"], (201, 202))


if __name__ == "__main__":
    unittest.main()
