import asyncio
import unittest
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from telegram import InputFile

from src.media import SPOOL_MEMORY_LIMIT, ByteBudget, MediaFile, media_item_budget


class MediaFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_project_temp_directory(self) -> None:
        with TemporaryDirectory() as directory:
            temp_dir = Path(directory) / "tmp"
            with (
                patch("src.paths.TEMP_DIR", temp_dir),
                patch(
                    "src.media.SpooledTemporaryFile",
                    wraps=SpooledTemporaryFile,
                ) as spool,
            ):
                media = await MediaFile.create()
                try:
                    media.write(b"x" * (SPOOL_MEMORY_LIMIT + 1))
                    self.assertTrue(temp_dir.is_dir())
                    spool.assert_called_once_with(
                        max_size=SPOOL_MEMORY_LIMIT,
                        mode="w+b",
                        dir=str(temp_dir),
                    )
                finally:
                    media.close()

    async def test_spool_rolls_over_after_memory_limit(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        try:
            media.write(b"x" * (SPOOL_MEMORY_LIMIT + 1))
            self.assertTrue(cast(Any, media.file)._rolled)
            self.assertEqual(media.size, SPOOL_MEMORY_LIMIT + 1)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_input_file_keeps_handle_when_requested(self) -> None:
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        try:
            media.write(b"image")
            media.rewind()
            upload = InputFile(
                media.file,
                filename=media.filename,
                read_file_handle=False,
            )
            self.assertIs(upload.input_file_content, media.file)
        finally:
            media.close()

    async def test_close_waits_for_active_stream(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        media.write(b"image")
        stream = media.chunks()
        self.assertEqual(await anext(stream), b"image")

        media.close()
        self.assertEqual(media_item_budget.used, initial_items + 1)
        await stream.aclose()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_unstarted_stream_can_be_closed(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        stream = media.chunks()
        media.close()
        self.assertEqual(media_item_budget.used, initial_items + 1)

        await stream.aclose()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_byte_budget_blocks_until_release(self) -> None:
        budget = ByteBudget(10)
        await budget.acquire(10)
        waiter = asyncio.create_task(budget.acquire(1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        await budget.release(10)
        await waiter
        self.assertEqual(budget.used, 1)
        await budget.release(1)


if __name__ == "__main__":
    unittest.main()
