import asyncio
import unittest
from functools import partial
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram import Message

from src.media import media_item_budget, media_queue_budget
from src.messages import TelegramMessage
from src.processing import ProcessingTask
from src.tgbot.handlers import TELEGRAM_DOWNLOAD_LIMIT, TELEGRAM_VIDEO_LIMIT, TGhandlers


class TelegramAlbumTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_flush_removes_pending_album(self) -> None:
        handler = TGhandlers()
        message = cast(Message, SimpleNamespace())
        handler._albums["album"] = [message]

        with patch("src.tgbot.handlers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await handler._flush_album("album")

        self.assertNotIn("album", handler._albums)
        self.assertNotIn("album", handler._album_tasks)

    async def test_mixed_media_group_is_downloaded_in_message_order(self) -> None:
        initial_items = media_item_budget.used
        initial_bytes = media_queue_budget.used

        async def download(request: httpx.Request) -> httpx.Response:
            content = b"video" if request.url.path.endswith("video") else b"image"
            return httpx.Response(200, content=content, request=request)

        video = SimpleNamespace(
            file_size=5,
            file_name="clip.mp4",
            mime_type="video/mp4",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=5,
                    file_path="https://example.test/video",
                )
            ),
        )
        photo = SimpleNamespace(
            file_size=5,
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=5,
                    file_path="https://example.test/image",
                )
            ),
        )
        user = SimpleNamespace(id=7, full_name="Telegram User")
        bot = SimpleNamespace()
        messages = [
            cast(
                Message,
                SimpleNamespace(
                    message_id=2,
                    chat_id=-456,
                    from_user=user,
                    video=video,
                    photo=(),
                    document=None,
                    caption=None,
                    reply_to_message=None,
                    get_bot=lambda: bot,
                ),
            ),
            cast(
                Message,
                SimpleNamespace(
                    message_id=1,
                    chat_id=-456,
                    from_user=user,
                    video=None,
                    photo=(photo,),
                    document=None,
                    caption="caption",
                    reply_to_message=None,
                    get_bot=lambda: bot,
                ),
            ),
        ]
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(transport=httpx.MockTransport(download))
        try:
            with (
                patch("src.tgbot.handlers.sql.get_tg_forward_enabled", new_callable=AsyncMock, return_value=True),
                patch("src.tgbot.handlers.media_processor.submit", return_value=True) as submit,
            ):
                await handler._enqueue_media(messages)

            task = submit.call_args.args[0]
            self.assertIsInstance(task, ProcessingTask)
            assert isinstance(task, ProcessingTask)
            self.assertIsInstance(task.run, partial)
            assert isinstance(task.run, partial)
            message = task.run.args[0]
            self.assertIsInstance(message, TelegramMessage)
            assert isinstance(message, TelegramMessage)
            self.assertEqual(message.message_ids, (1, 2))
            self.assertEqual([item.kind for item in message.media], ["image", "video"])
            self.assertEqual(message.text, "caption")
            await task.cleanup()
        finally:
            await handler.download_client.aclose()

        self.assertEqual(media_item_budget.used, initial_items)
        self.assertEqual(media_queue_budget.used, initial_bytes)

    async def test_oversized_video_is_rejected_before_download(self) -> None:
        video = SimpleNamespace(
            file_size=TELEGRAM_VIDEO_LIMIT + 1,
            get_file=AsyncMock(),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=1,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                video=video,
                photo=(),
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

        video.get_file.assert_not_awaited()

    async def test_document_is_forwarded_as_onebot_file(self) -> None:
        initial_items = media_item_budget.used
        initial_bytes = media_queue_budget.used
        document = SimpleNamespace(
            file_size=4,
            file_name="archive.zip",
            mime_type="application/zip",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=4,
                    file_path="https://example.test/file",
                )
            ),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=3,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                video=None,
                photo=(),
                document=document,
                caption="file caption",
                reply_to_message=None,
                get_bot=lambda: SimpleNamespace(),
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"file", request=request)
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
            self.assertEqual(forwarded.media[0].kind, "file")
            self.assertEqual(forwarded.media[0].content.filename, "archive.zip")
            self.assertEqual(forwarded.text, "file caption")
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        self.assertEqual(media_item_budget.used, initial_items)
        self.assertEqual(media_queue_budget.used, initial_bytes)

    async def test_audio_is_forwarded_as_onebot_file(self) -> None:
        initial_items = media_item_budget.used
        initial_bytes = media_queue_budget.used
        audio = SimpleNamespace(
            file_size=5,
            file_name="example-song.mp3",
            mime_type="audio/mpeg",
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=5,
                    file_path="https://example.test/audio",
                )
            ),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=5,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                sticker=None,
                video=None,
                voice=None,
                photo=(),
                audio=audio,
                document=None,
                caption="audio caption",
                reply_to_message=None,
                get_bot=lambda: SimpleNamespace(),
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"audio", request=request)
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
            self.assertEqual(forwarded.media[0].kind, "file")
            self.assertEqual(forwarded.media[0].content.filename, "example-song.mp3")
            self.assertEqual(forwarded.media[0].content.media_type, "audio/mpeg")
            self.assertEqual(forwarded.text, "audio caption")
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        self.assertEqual(media_item_budget.used, initial_items)
        self.assertEqual(media_queue_budget.used, initial_bytes)

    async def test_photo_over_10_mb_is_allowed_up_to_download_limit(self) -> None:
        size = 10_000_001
        photo = SimpleNamespace(
            file_size=size,
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_size=size,
                    file_path="https://example.test/image",
                )
            ),
        )
        message = cast(
            Message,
            SimpleNamespace(
                message_id=4,
                chat_id=-456,
                from_user=SimpleNamespace(id=7, full_name="Telegram User"),
                video=None,
                photo=(photo,),
                document=None,
                caption=None,
                reply_to_message=None,
                get_bot=lambda: SimpleNamespace(),
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"image", request=request)
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
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        self.assertLess(size, TELEGRAM_DOWNLOAD_LIMIT)


if __name__ == "__main__":
    unittest.main()
