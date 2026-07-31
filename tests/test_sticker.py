import asyncio
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from src.media import MediaFile, media_item_budget
from src.sticker import static_sticker_to_png, video_sticker_to_gif


class StickerConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_webp_is_converted_to_rgba_png(self) -> None:
        initial_items = media_item_budget.used
        source = io.BytesIO()
        Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(source, format="WEBP")
        media = await MediaFile.create(filename="sticker.webp", media_type="image/webp")
        media.write(source.getvalue())
        try:
            with (
                patch(
                    "src.sticker.time",
                    SimpleNamespace(monotonic=Mock(side_effect=[20.0, 20.5])),
                ),
                patch("src.sticker.baselog.info") as info,
            ):
                await static_sticker_to_png(media, size_limit=20_000_000)
            self.assertEqual(media.filename, "sticker.png")
            self.assertEqual(media.media_type, "image/png")
            self.assertEqual(media.file.read(8), b"\x89PNG\r\n\x1a\n")
            media.rewind()
            with Image.open(media.file) as converted:
                self.assertEqual(converted.mode, "RGBA")
                self.assertEqual(converted.size, (16, 16))
            info.assert_called_once_with("静态贴纸转换完成，耗时 %.2f 秒", 0.5)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_video_webm_sticker_is_converted_to_gif(self) -> None:
        initial_items = media_item_budget.used
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sticker.webm"
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red@0.5:s=64x64:d=0.2,format=rgba",
                "-an",
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                str(path),
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            source = path.read_bytes()

        media = await MediaFile.create(filename="sticker.webm", media_type="video/webm")
        media.write(source)
        try:
            with (
                patch(
                    "src.sticker.time",
                    SimpleNamespace(monotonic=Mock(side_effect=[30.0, 31.25])),
                ),
                patch("src.sticker.baselog.info") as info,
            ):
                await video_sticker_to_gif(media, size_limit=20_000_000)
            self.assertEqual(media.filename, "sticker.gif")
            self.assertEqual(media.media_type, "image/gif")
            self.assertIn(media.file.read(6), {b"GIF87a", b"GIF89a"})
            media.rewind()
            with Image.open(media.file) as converted:
                self.assertEqual(converted.format, "GIF")
            info.assert_called_once_with("视频贴纸转码完成，耗时 %.2f 秒", 1.25)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)


if __name__ == "__main__":
    unittest.main()
