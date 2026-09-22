from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telegram import Message

from src.media import (
    MEDIA_MEMORY_TIER_LIMIT,
    media_item_budget,
    media_memory_budget,
    media_queue_budget,
)
from src.messages import TelegramMessage
from src.processing import ProcessingTask
from src.tgbot.handlers import TELEGRAM_DOWNLOAD_LIMIT, TELEGRAM_VIDEO_LIMIT, TGhandlers


@pytest.mark.asyncio
class TestTelegramAlbum:
    async def test_media_group_state_is_not_kept(self) -> None:
        # 相册不再聚合：handler 不再持有 _albums / _album_tasks / _flush_album。
        handler = TGhandlers()
        assert not hasattr(handler, "_albums")
        assert not hasattr(handler, "_album_tasks")
        assert not hasattr(handler, "_flush_album")

    async def test_each_album_item_is_downloaded_separately(self) -> None:
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
        video_message = cast(
            Message,
            SimpleNamespace(
                message_id=2,
                chat_id=-456,
                from_user=user,
                media_group_id="album",
                video=video,
                photo=(),
                document=None,
                sticker=None,
                voice=None,
                audio=None,
                caption=None,
                reply_to_message=None,
                get_bot=lambda: bot,
            ),
        )
        photo_message = cast(
            Message,
            SimpleNamespace(
                message_id=1,
                chat_id=-456,
                from_user=user,
                media_group_id="album",
                video=None,
                photo=(photo,),
                document=None,
                sticker=None,
                voice=None,
                audio=None,
                caption="caption",
                reply_to_message=None,
                get_bot=lambda: bot,
            ),
        )
        handler = TGhandlers()
        handler.download_client = httpx.AsyncClient(transport=httpx.MockTransport(download))
        try:
            with (
                patch("src.tgbot.handlers.sql.get_tg_forward_enabled", new_callable=AsyncMock, return_value=True),
                patch("src.tgbot.handlers.media_processor.submit", return_value=True) as submit,
                patch("src.tgbot.handlers.message_bus.put", new_callable=AsyncMock) as put,
            ):
                await handler._enqueue_media(video_message)
                await handler._enqueue_media(photo_message)

            # 视频需要预处理，图片直接入队；两条相册项各自独立转发。
            assert submit.call_count == 1
            assert put.await_count == 1

            assert submit.call_args is not None
            task = submit.call_args.args[0]
            assert isinstance(task, ProcessingTask)
            assert isinstance(task.run, partial)
            video_forwarded = task.run.args[0]
            assert isinstance(video_forwarded, TelegramMessage)
            await task.cleanup()

            assert put.await_args is not None
            photo_task = put.await_args.args[0]
            photo_forwarded = photo_task.send.args[0]
            assert isinstance(photo_forwarded, TelegramMessage)
            assert photo_task.finalize is not None
            await photo_task.finalize()

            # 每条相册项各自成为一条消息，message_ids 不再合并成元组。
            assert video_forwarded.message_ids == (2,)
            assert photo_forwarded.message_ids == (1,)
            assert video_forwarded.media[0].kind == "video"
            assert photo_forwarded.media[0].kind == "image"
            assert photo_forwarded.text == "caption"
        finally:
            await handler.download_client.aclose()

        assert media_item_budget.used == initial_items
        assert media_queue_budget.used == initial_bytes

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
            pytest.raises(ValueError, match="媒体超过 20 MB，无法转发"),
        ):
            await handler._enqueue_media(message)

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
                await handler._enqueue_media(message)

            assert put.await_args is not None
            task = put.await_args.args[0]
            forwarded = task.send.args[0]
            assert forwarded.media[0].kind == "file"
            assert forwarded.media[0].content.filename == "archive.zip"
            assert forwarded.text == "file caption"
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        assert media_item_budget.used == initial_items
        assert media_queue_budget.used == initial_bytes

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
                await handler._enqueue_media(message)

            assert put.await_args is not None
            task = put.await_args.args[0]
            forwarded = task.send.args[0]
            assert forwarded.media[0].kind == "file"
            assert forwarded.media[0].content.filename == "example-song.mp3"
            assert forwarded.media[0].content.media_type == "audio/mpeg"
            assert forwarded.text == "audio caption"
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        assert media_item_budget.used == initial_items
        assert media_queue_budget.used == initial_bytes

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
                await handler._enqueue_media(message)
            assert put.await_args is not None
            task = put.await_args.args[0]
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        assert size < TELEGRAM_DOWNLOAD_LIMIT

    async def test_middle_tier_file_size_keeps_download_in_memory(self) -> None:
        # get_file 的 file_size 必须作为 expected_size 传给分档，否则入站媒体
        # 永远只能走 1 MiB 免费档，内存档对 Telegram -> OneBot 方向就没有作用。
        initial_items = media_item_budget.used
        initial_memory = media_memory_budget.used
        size = 3 * 1024 * 1024
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
                message_id=6,
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
                await handler._enqueue_media(message)
            assert put.await_args is not None
            task = put.await_args.args[0]
            content = task.send.args[0].media[0].content
            assert media_memory_budget.used == initial_memory + size
            assert not cast(Any, content.file)._rolled
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        assert media_memory_budget.used == initial_memory
        assert media_item_budget.used == initial_items

    async def test_large_file_size_spools_download_to_disk(self) -> None:
        initial_items = media_item_budget.used
        initial_memory = media_memory_budget.used
        size = MEDIA_MEMORY_TIER_LIMIT + 1
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
                message_id=7,
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
                await handler._enqueue_media(message)
            assert put.await_args is not None
            task = put.await_args.args[0]
            content = task.send.args[0].media[0].content
            # 声明大小超过中间档上限，创建时就落盘且不占额度。
            assert cast(Any, content.file)._rolled
            assert media_memory_budget.used == initial_memory
            assert task.finalize is not None
            await task.finalize()
        finally:
            await handler.download_client.aclose()

        assert size < TELEGRAM_DOWNLOAD_LIMIT
        assert media_item_budget.used == initial_items
