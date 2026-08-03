import json
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram import InputFile, ReplyParameters
from telegram.ext import ExtBot

from src.forwarding import forward_onebot_to_telegram, onebot_group_announcement
from src.media import MediaFile
from src.messages import OneBotMessage


def announcement_segment() -> dict:
    return {
        "type": "json",
        "data": {
            "data": json.dumps(
                {
                    "app": "com.tencent.mannounce",
                    "meta": {
                        "mannounce": {
                            "encode": 1,
                            "title": "576k5YWs5ZGK",
                            "text": "5rWL6K+VCua1i+ivlTE=",
                            "pic": [
                                {
                                    "height": 640,
                                    "width": 640,
                                    "url": "announcement-image-id",
                                }
                            ],
                        }
                    },
                    "prompt": "[群公告]测试测试1",
                },
                ensure_ascii=False,
            )
        },
    }


class GroupAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def test_encoded_announcement_is_decoded(self) -> None:
        self.assertEqual(
            onebot_group_announcement([announcement_segment()]),
            (
                "测试\n测试1",
                "https://gdynamic.qpic.cn/gdynamic/announcement-image-id/0",
            ),
        )

    def test_unrelated_or_malformed_json_is_ignored(self) -> None:
        self.assertIsNone(
            onebot_group_announcement(
                [
                    {"type": "json", "data": {"data": "not-json"}},
                    {
                        "type": "json",
                        "data": {"data": json.dumps({"app": "example.test"})},
                    },
                ]
            )
        )

    async def test_announcement_is_sent_as_markdown_file_and_reuses_filename(
        self,
    ) -> None:
        bot = SimpleNamespace(
            send_document=AsyncMock(
                side_effect=[
                    RuntimeError("temporary failure"),
                    SimpleNamespace(message_id=201),
                ]
            ),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=202)),
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=456,
            sender_name="测试群名片",
            message=[announcement_segment()],
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-100_123),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(),
        )

        image = await MediaFile.create(
            filename="群公告图片.jpg",
            media_type="image/jpeg",
        )
        image.write(b"image")
        image.rewind()
        try:
            with (
                patch("src.forwarding.sql", database),
                patch(
                    "src.forwarding.token_hex", return_value="0123456789abcdef"
                ) as token,
                patch(
                    "src.forwarding.download_image",
                    new_callable=AsyncMock,
                    return_value=image,
                ) as download,
            ):
                with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                    await forward_onebot_to_telegram(
                        message,
                        cast(ExtBot[None], bot),
                        cast(httpx.AsyncClient, SimpleNamespace()),
                    )
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    cast(httpx.AsyncClient, SimpleNamespace()),
                )
        finally:
            image.close()

        token.assert_called_once_with(8)
        self.assertEqual(bot.send_document.await_count, 2)
        for call in bot.send_document.await_args_list:
            document = call.kwargs["document"]
            self.assertIsInstance(document, InputFile)
            self.assertEqual(document.filename, "群公告 - 0123456789abcdef.md")
            self.assertEqual(
                document.input_file_content,
                (
                    "测试群名片:\n\n"
                    "# 群公告\n\n"
                    "测试\n测试1\n"
                ).encode(),
            )
            self.assertEqual(call.kwargs["caption"], "测试群名片:")
        download.assert_awaited_once_with(
            cast(httpx.AsyncClient, SimpleNamespace()),
            "https://gdynamic.qpic.cn/gdynamic/announcement-image-id/0",
            filename="群公告图片.jpg",
        )
        bot.send_photo.assert_awaited_once()
        image_reply = bot.send_photo.await_args.kwargs["reply_parameters"]
        self.assertIsInstance(image_reply, ReplyParameters)
        self.assertEqual(image_reply.message_id, 201)
        self.assertTrue(image_reply.allow_sending_without_reply)
        self.assertEqual(message.tg_message_ids, [201, 202])
        database.set_message_mapping.assert_awaited_once()

    async def test_missing_nickname_and_image_use_expected_fallback(self) -> None:
        segment = announcement_segment()
        payload = json.loads(segment["data"]["data"])
        del payload["meta"]["mannounce"]["pic"]
        segment["data"]["data"] = json.dumps(payload, ensure_ascii=False)
        bot = SimpleNamespace(
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=201))
        )
        message = OneBotMessage(
            message_id=102,
            group_id=123,
            user_id=456,
            sender_name="OneBot 用户[456]",
            sender_name_is_fallback=True,
            message=[segment],
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-100_123),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(),
        )

        with patch("src.forwarding.sql", database):
            await forward_onebot_to_telegram(
                message,
                cast(ExtBot[None], bot),
                cast(httpx.AsyncClient, SimpleNamespace()),
            )

        document = bot.send_document.await_args.kwargs["document"]
        self.assertEqual(
            document.input_file_content,
            "OneBot 用户:\n\n# 群公告\n\n测试\n测试1\n".encode(),
        )
        self.assertEqual(bot.send_document.await_args.kwargs["caption"], "OneBot 用户:")

    async def test_image_retry_replies_to_existing_announcement_document(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"image", request=request)

        bot = SimpleNamespace(
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=301)),
            send_photo=AsyncMock(
                side_effect=[
                    RuntimeError("temporary image failure"),
                    SimpleNamespace(message_id=302),
                ]
            ),
        )
        message = OneBotMessage(
            message_id=103,
            group_id=123,
            user_id=456,
            sender_name="测试群名片",
            message=[announcement_segment()],
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-100_123),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(),
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("src.forwarding.sql", database):
                with self.assertRaisesRegex(RuntimeError, "temporary image failure"):
                    await forward_onebot_to_telegram(
                        message,
                        cast(ExtBot[None], bot),
                        client,
                    )
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_document.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_count, 2)
        for call in bot.send_photo.await_args_list:
            reply = call.kwargs["reply_parameters"]
            self.assertEqual(reply.message_id, 301)
            self.assertTrue(reply.allow_sending_without_reply)
        self.assertEqual(message.tg_message_ids, [301, 302])
        database.set_message_mapping.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
