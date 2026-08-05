from typing import Any, cast

import httpx
import pytest
from telegram import InputFile

from src.forwarding import ONEBOT_MEDIA_LIMIT, download_image
from src.media import MEDIA_MEMORY_TIER_LIMIT, media_item_budget, media_memory_budget
from src.messages import MediaTooLargeError


@pytest.mark.asyncio
class TestMessageBusMedia:
    async def test_download_image_streams_into_spool(self) -> None:
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"image", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = await download_image(client, "https://example.test/image", filename="image.jpg")
        try:
            assert media.size == 5
            assert media.file.read() == b"image"
        finally:
            media.close()
        assert media_item_budget.used == initial_items

    async def test_download_rejects_oversized_content_length(self) -> None:
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(ONEBOT_MEDIA_LIMIT + 1)},
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MediaTooLargeError, match="50 MB，无法转发"):
                await download_image(client, "https://example.test/image", filename="image.jpg")
        assert media_item_budget.used == initial_items

    async def test_download_passes_content_length_to_memory_tier(self) -> None:
        # 出站下载必须把 Content-Length 传给分档，否则中间档永远拿不到额度。
        initial_items = media_item_budget.used
        initial_memory = media_memory_budget.used
        declared = 3 * 1024 * 1024

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(declared)},
                content=b"x" * declared,
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = await download_image(client, "https://example.test/image", filename="image.jpg")
        try:
            assert media.size == declared
            # 声明大小落在中间档，应占用额度并留在内存。
            assert media_memory_budget.used == initial_memory + declared
            assert not cast(Any, media.file)._rolled
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory
        assert media_item_budget.used == initial_items

    async def test_download_spools_large_declared_size_to_disk(self) -> None:
        initial_items = media_item_budget.used
        initial_memory = media_memory_budget.used
        declared = MEDIA_MEMORY_TIER_LIMIT + 1

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(declared)},
                content=b"x" * declared,
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = await download_image(client, "https://example.test/image", filename="image.jpg")
        try:
            assert media.size == declared
            # 超过中间档上限的媒体不占额度，创建时就已落盘。
            assert cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory
        finally:
            media.close()
        assert media_item_budget.used == initial_items

    async def test_download_releases_item_slot_when_response_fails(self) -> None:
        # 文件名额在建立连接之前取得，连接失败时必须归还，否则名额会泄漏。
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(RuntimeError, match="OneBot 媒体下载失败"):
                await download_image(client, "https://example.test/image", filename="image.jpg")
        assert media_item_budget.used == initial_items

    async def test_ptb_upload_uses_file_handle(self) -> None:
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"image", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = await download_image(client, "https://example.test/image", filename="image.jpg")
        try:
            upload = InputFile(
                media.file,
                filename=media.filename,
                read_file_handle=False,
            )
            assert upload.input_file_content is media.file
        finally:
            media.close()
        assert media_item_budget.used == initial_items
