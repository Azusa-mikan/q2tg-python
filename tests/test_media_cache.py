import time
import unittest

from src.media import CachedMedia, MediaCache, MediaFile, media_item_budget


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_storage_and_close(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        first = await MediaFile.create(filename="first.jpg", media_type="image/jpeg")
        second = await MediaFile.create(filename="second.jpg", media_type="image/jpeg")
        first.write(b"first")
        second.write(b"second")

        media_ids = cache.set_media_batch((first, second))
        self.assertEqual(len(media_ids), 2)
        self.assertIs(cache.get_media(media_ids[0]), first)
        self.assertEqual(cache._media_bytes, 11)

        cache.close()
        self.assertEqual(cache._media_bytes, 0)
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_expired_media_closes_after_stream_finishes(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        media.write(b"image")
        media_id = cache.set_media_batch((media,))[0]
        stream = media.chunks()
        try:
            self.assertEqual(await anext(stream), b"image")
            cache._media[media_id] = CachedMedia(
                content=media,
                expires_at=time.time() - 1,
            )
            cache.purge_expired()
            self.assertIsNone(cache.get_media(media_id))
            self.assertEqual(cache._media_bytes, 5)
            self.assertEqual(media_item_budget.used, initial_items + 1)
        finally:
            await stream.aclose()
            cache.close()
        self.assertEqual(cache._media_bytes, 0)
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_pinned_media_survives_ttl_until_task_releases_it(self) -> None:
        initial_items = media_item_budget.used
        cache = MediaCache()
        media = await MediaFile.create(filename="voice.ogg", media_type="audio/ogg")
        media.write(b"voice")
        media_id = cache.set_media_batch((media,), pinned=True)[0]
        cache._media[media_id].expires_at = time.time() - 1

        cache.purge_expired()
        self.assertIs(cache.get_media(media_id), media)
        cache.release_media_batch((media_id,))
        self.assertGreater(cache._media[media_id].expires_at, time.time())

        cache.close()
        self.assertEqual(media_item_budget.used, initial_items)


if __name__ == "__main__":
    unittest.main()
