import time

import pytest

from src.media import CachedMedia, MediaCache, MediaFile, media_item_budget


@pytest.mark.asyncio
class TestMediaCache:
    async def test_batch_storage_and_close(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        first = await MediaFile.create(filename="first.jpg", media_type="image/jpeg")
        second = await MediaFile.create(filename="second.jpg", media_type="image/jpeg")
        first.write(b"first")
        second.write(b"second")

        media_ids = cache.set_media_batch((first, second))
        assert len(media_ids) == 2
        assert cache.get_media(media_ids[0]) is first
        assert cache._media_bytes == 11

        cache.close()
        assert cache._media_bytes == 0
        assert media_item_budget.used == initial_items

    async def test_expired_media_closes_after_stream_finishes(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        media.write(b"image")
        media_id = cache.set_media_batch((media,))[0]
        stream = media.chunks()
        try:
            assert await anext(stream) == b"image"
            cache._media[media_id] = CachedMedia(
                content=media,
                expires_at=time.time() - 1,
            )
            cache.purge_expired()
            assert cache.get_media(media_id) is None
            assert cache._media_bytes == 5
            assert media_item_budget.used == initial_items + 1
        finally:
            await stream.aclose()
            cache.close()
        assert cache._media_bytes == 0
        assert media_item_budget.used == initial_items

    async def test_pinned_media_survives_ttl_until_task_releases_it(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        media = await MediaFile.create(filename="voice.ogg", media_type="audio/ogg")
        media.write(b"voice")
        media_id = cache.set_media_batch((media,), pinned=True)[0]
        cache._media[media_id].expires_at = time.time() - 1

        cache.purge_expired()
        assert cache.get_media(media_id) is media
        cache.release_media_batch((media_id,))
        assert cache._media[media_id].expires_at > time.time()

        cache.close()
        assert media_item_budget.used == initial_items
