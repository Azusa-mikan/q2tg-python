import io
import unittest
from functools import partial
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from telegram import Message

from src.media import media_item_budget, media_queue_budget
from src.messages import TelegramMessage
from src.processing import ProcessingTask
from src.tgbot.handlers import TGhandlers


class TelegramStickerTests(unittest.IsolatedAsyncioTestCase):
    def _webp(self) -> bytes:
        output = io.BytesIO()
        Image.new("RGBA", (16, 16), (0, 255, 0, 128)).save(output, format="WEBP")
        return output.getvalue()

    async def _enqueue_sticker(self, sticker, content: bytes) -> tuple[ProcessingTask, TelegramMessage]:
        message = cast(
            Message,
            SimpleNamespace(
                message_id=10,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                sticker=sticker,
                video=None,
                photo=(),
                document=None,
                caption=None,
                reply_to_message=SimpleNamespace(message_id=9),
                get_bot=lambda: SimpleNamespace(),
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=content, request=request)
            )
        )
        try:
            with (
                patch(
                    "src.tgbot.handlers.sql.get_tg_forward_enabled",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch("src.tgbot.handlers.media_processor.submit", return_value=True) as submit,
            ):
                await handler._enqueue_media([message])
            task = submit.call_args.args[0]
            self.assertIsInstance(task, ProcessingTask)
            assert isinstance(task, ProcessingTask)
            self.assertIsInstance(task.run, partial)
            assert isinstance(task.run, partial)
            forwarded = task.run.args[0]
            self.assertIsInstance(forwarded, TelegramMessage)
            assert isinstance(forwarded, TelegramMessage)
            return task, forwarded
        finally:
            await handler.download_client.aclose()

    async def test_static_sticker_uses_original_webp(self) -> None:
        initial_items = media_item_budget.used
        initial_bytes = media_queue_budget.used
        sticker = SimpleNamespace(
            is_animated=False,
            is_video=False,
            file_size=100,
            file_name="sticker.webp",
            mime_type="image/webp",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=100,
                    file_path="https://example.test/sticker",
                )
            ),
        )
        task, message = await self._enqueue_sticker(sticker, self._webp())
        self.assertEqual(message.media[0].kind, "image")
        self.assertEqual(message.media[0].processing, "sticker_static")
        self.assertEqual(message.reply_message_id, 9)
        await task.cleanup()
        self.assertEqual(media_item_budget.used, initial_items)
        self.assertEqual(media_queue_budget.used, initial_bytes)

    async def test_tgs_sticker_downloads_only_thumbnail(self) -> None:
        thumbnail = SimpleNamespace(
            file_size=100,
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=100,
                    file_path="https://example.test/thumbnail",
                )
            ),
        )
        sticker = SimpleNamespace(
            is_animated=True,
            is_video=False,
            thumbnail=thumbnail,
            get_file=AsyncMock(),
        )
        task, message = await self._enqueue_sticker(sticker, self._webp())
        sticker.get_file.assert_not_awaited()
        thumbnail.get_file.assert_awaited_once()
        self.assertEqual(message.media[0].processing, "sticker_static")
        await task.cleanup()

    async def test_video_sticker_is_scheduled_for_gif_conversion(self) -> None:
        sticker = SimpleNamespace(
            is_animated=False,
            is_video=True,
            file_size=100,
            file_name="sticker.webm",
            mime_type="video/webm",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=100,
                    file_path="https://example.test/sticker",
                )
            ),
        )
        task, message = await self._enqueue_sticker(sticker, b"webm")
        self.assertEqual(message.media[0].kind, "image")
        self.assertEqual(message.media[0].processing, "sticker_video")
        await task.cleanup()


if __name__ == "__main__":
    unittest.main()
