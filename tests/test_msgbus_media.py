import unittest

import httpx
from telegram import InputFile

from src.forwarding import ONEBOT_MEDIA_LIMIT, download_image
from src.media import media_item_budget
from src.messages import MediaTooLargeError


class MessageBusMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_image_streams_into_spool(self) -> None:
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"image", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            media = await download_image(client, "https://example.test/image", filename="image.jpg")
        try:
            self.assertEqual(media.size, 5)
            self.assertEqual(media.file.read(), b"image")
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_download_rejects_oversized_content_length(self) -> None:
        initial_items = media_item_budget.used

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(ONEBOT_MEDIA_LIMIT + 1)},
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(MediaTooLargeError, "20 MB，无法转发"):
                await download_image(client, "https://example.test/image", filename="image.jpg")
        self.assertEqual(media_item_budget.used, initial_items)

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
            self.assertIs(upload.input_file_content, media.file)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)


if __name__ == "__main__":
    unittest.main()
