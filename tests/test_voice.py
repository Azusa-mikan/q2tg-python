import math
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pilk
from telegram import Message
from telegram.ext import ExtBot

from src.audio import normalize_onebot_record
from src.forwarding import forward_onebot_to_telegram, forward_telegram_to_onebot
from src.media import MediaFile, media_cache, media_item_budget, media_queue_budget
from src.messages import OneBotMessage, TelegramMedia, TelegramMessage
from src.qbot import QGateway
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = Sql(Path(self.directory.name) / "voice.sqlite3")
        await self.database.load()
        await self.database.bind_group(123, -456)

    async def asyncTearDown(self) -> None:
        media_cache.close()
        await self.database.close()
        self.directory.cleanup()

    async def test_silk_is_converted_to_ogg_opus(self) -> None:
        pcm_path = Path(self.directory.name) / "voice.pcm"
        silk_path = Path(self.directory.name) / "voice.silk"
        samples = (
            int(12000 * math.sin(2 * math.pi * 440 * index / 24000))
            for index in range(2400)
        )
        pcm_path.write_bytes(b"".join(struct.pack("<h", sample) for sample in samples))
        pilk.encode(str(pcm_path), str(silk_path), pcm_rate=24000, tencent=True)

        media = await MediaFile.create(filename="voice.amr", media_type="application/octet-stream")
        media.write(silk_path.read_bytes())
        media.rewind()
        try:
            await normalize_onebot_record(media)
            self.assertEqual(media.filename, "voice.ogg")
            self.assertEqual(media.media_type, "audio/ogg")
            self.assertEqual(media.file.read(4), b"OggS")
        finally:
            media.close()

    async def test_existing_ogg_opus_is_kept_without_silk_decode(self) -> None:
        pcm_path = Path(self.directory.name) / "passthrough.pcm"
        silk_path = Path(self.directory.name) / "passthrough.silk"
        samples = (0 for _ in range(2400))
        pcm_path.write_bytes(b"".join(struct.pack("<h", sample) for sample in samples))
        pilk.encode(str(pcm_path), str(silk_path), pcm_rate=24000, tencent=True)
        media = await MediaFile.create(filename="voice.silk", media_type="audio/silk")
        media.write(silk_path.read_bytes())
        media.rewind()
        try:
            await normalize_onebot_record(media)
            original = media.file.read()
            media.rewind()
            await normalize_onebot_record(media)
            self.assertEqual(media.file.read(), original)
            self.assertEqual(media.filename, "voice.ogg")
            self.assertEqual(media.media_type, "audio/ogg")
        finally:
            media.close()

    async def test_onebot_record_is_sent_as_telegram_voice(self) -> None:
        async def download(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"silk", request=request)

        bot = SimpleNamespace(
            send_voice=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=1,
            sender_name="OneBot User",
            message=[
                {
                    "type": "record",
                    "data": {
                        "file": "voice.amr",
                        "url": "https://example.test/voice",
                    },
                }
            ],
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
            with (
                patch("src.forwarding.sql", self.database),
                patch("src.forwarding.normalize_onebot_record", new_callable=AsyncMock) as convert,
            ):
                convert.side_effect = lambda media: setattr(media, "filename", "voice.ogg")
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_voice.assert_awaited_once()
        self.assertEqual(bot.send_voice.await_args.kwargs["caption"], "OneBot User[1]:")
        self.assertEqual(bot.send_voice.await_args.kwargs["voice"].filename, "voice.ogg")

    async def test_record_without_url_sends_visible_placeholder(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=202)),
        )
        message = OneBotMessage(
            message_id=102,
            group_id=123,
            user_id=1,
            sender_name="OneBot User",
            message=[{"type": "record", "data": {"file": "voice.silk"}}],
        )

        async with httpx.AsyncClient() as client:
            with patch("src.forwarding.sql", self.database):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_awaited_once_with(
            chat_id=-456,
            text="OneBot User[1]:\n[语音无法转发：缺少可用的 HTTP(S) 下载地址]",
            reply_parameters=None,
        )

    async def test_voice_caption_over_limit_is_sent_as_retry_safe_text(self) -> None:
        async def download(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"silk", request=request)

        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=203)),
            send_voice=AsyncMock(
                side_effect=[RuntimeError("send failed"), SimpleNamespace(message_id=204)]
            ),
        )
        message = OneBotMessage(
            message_id=103,
            group_id=123,
            user_id=1,
            sender_name="OneBot User",
            message=[
                {"type": "text", "data": {"text": "x" * 1024}},
                {
                    "type": "record",
                    "data": {"file": "voice.silk", "url": "https://example.test/voice"},
                },
            ],
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
            with (
                patch("src.forwarding.sql", self.database),
                patch("src.forwarding.normalize_onebot_record", new_callable=AsyncMock),
            ):
                with self.assertRaisesRegex(RuntimeError, "send failed"):
                    await forward_onebot_to_telegram(message, cast(ExtBot[None], bot), client)
                await forward_onebot_to_telegram(message, cast(ExtBot[None], bot), client)

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_voice.await_count, 2)
        self.assertIsNone(bot.send_voice.await_args.kwargs["caption"])
        mapping = await self.database.get_tg_message(123, 103)
        assert mapping is not None
        self.assertEqual(mapping.tg_message_ids, (203, 204))

    async def test_telegram_voice_uses_record_url_and_separate_text(self) -> None:
        voice = await MediaFile.create(filename="voice.ogg", media_type="audio/ogg")
        voice.write(b"OggSvoice")
        gateway = SimpleNamespace(send_group_message=AsyncMock(side_effect=[99, 100]))
        message = TelegramMessage(
            message_ids=(200,),
            group_id=-456,
            user_id=2,
            sender_name="Telegram User",
            text="caption",
            media=(TelegramMedia(kind="record", content=voice),),
        )

        with patch("src.forwarding.sql", self.database):
            await forward_telegram_to_onebot(message, cast(QGateway, gateway))

        self.assertEqual(gateway.send_group_message.await_count, 2)
        self.assertEqual(
            gateway.send_group_message.await_args_list[0].kwargs["message"],
            [{"type": "text", "data": {"text": "Telegram User:\ncaption"}}],
        )
        record = gateway.send_group_message.await_args_list[1].kwargs["message"][0]
        self.assertEqual(record["type"], "record")
        self.assertIn("/media/", record["data"]["file"])

    async def test_telegram_voice_without_caption_has_no_trailing_newline(self) -> None:
        voice = await MediaFile.create(filename="voice.ogg", media_type="audio/ogg")
        voice.write(b"OggSvoice")
        gateway = SimpleNamespace(send_group_message=AsyncMock(side_effect=[99, 100]))
        message = TelegramMessage(
            message_ids=(201,),
            group_id=-456,
            user_id=2,
            sender_name="Telegram User",
            text=None,
            media=(TelegramMedia(kind="record", content=voice),),
        )

        with patch("src.forwarding.sql", self.database):
            await forward_telegram_to_onebot(message, cast(QGateway, gateway))

        self.assertEqual(
            gateway.send_group_message.await_args_list[0].kwargs["message"],
            [{"type": "text", "data": {"text": "Telegram User:"}}],
        )

    async def test_telegram_voice_is_downloaded_without_processing(self) -> None:
        initial_items = media_item_budget.used
        initial_bytes = media_queue_budget.used
        source = SimpleNamespace(
            file_size=9,
            mime_type="audio/ogg",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=9,
                    file_path="https://example.test/voice.ogg",
                )
            ),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=3,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                sticker=None,
                video=None,
                voice=source,
                photo=(),
                document=None,
                caption=None,
                reply_to_message=None,
                get_bot=lambda: SimpleNamespace(),
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"OggSvoice", request=request)
            )
        )
        try:
            with (
                patch(
                    "src.tgbot.handlers.sql.get_tg_forward_enabled",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch("src.tgbot.handlers.message_bus.put", new_callable=AsyncMock) as put,
            ):
                await handler._enqueue_media([message])

            assert put.await_args is not None
            task = put.await_args.args[0]
            forwarded = task.send.args[0]
            self.assertEqual(forwarded.media[0].kind, "record")
            self.assertEqual(forwarded.media[0].processing, "none")
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        self.assertEqual(media_item_budget.used, initial_items)
        self.assertEqual(media_queue_budget.used, initial_bytes)

    async def test_telegram_voice_over_20_mb_is_rejected_before_download(self) -> None:
        source = SimpleNamespace(
            file_size=20_000_001,
            mime_type="audio/ogg",
            get_file=AsyncMock(),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=4,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                sticker=None,
                video=None,
                voice=source,
                photo=(),
                document=None,
                caption=None,
                reply_to_message=None,
            ),
        )
        handler = TGhandlers()
        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            self.assertRaisesRegex(ValueError, "媒体超过 20 MB，无法转发"),
        ):
            await handler._enqueue_media([message])

        source.get_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
