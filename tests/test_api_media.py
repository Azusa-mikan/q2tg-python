import unittest

from fastapi import HTTPException
from starlette.responses import StreamingResponse

from src.api import MediaResponse, get_media
from src.media import MediaFile, media_cache, media_item_budget


class ApiMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_endpoint_streams_spooled_file(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        media.write(b"image")
        media_id = media_cache.set_media_batch((media,))[0]
        try:
            response = await get_media(media_id)
            self.assertIsInstance(response, StreamingResponse)
            assert isinstance(response, MediaResponse)
            self.assertEqual(response.headers["content-length"], "5")
            self.assertEqual(
                response.headers["content-disposition"],
                "attachment; filename*=UTF-8''image.jpg",
            )
            chunks = [
                chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                async for chunk in response.body_iterator
            ]
            self.assertEqual(b"".join(chunks), b"image")
        finally:
            media_cache.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_media_endpoint_returns_404_for_unknown_id(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await get_media("missing")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
