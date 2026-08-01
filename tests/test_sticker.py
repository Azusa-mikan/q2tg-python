import asyncio
import gzip
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from src.media import MediaFile, media_item_budget
from src.sticker import static_sticker_to_png, tgs_sticker_to_gif, video_sticker_to_gif


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
                (
                    "color=c=black@0.0:s=64x64:d=0.2,format=rgba,"
                    "drawbox=x=16:y=16:w=32:h=32:color=red@1:t=fill:replace=1"
                ),
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
                rgba = converted.convert("RGBA")
                alpha = rgba.getchannel("A")
                self.assertEqual(alpha.getpixel((0, 0)), 0)
                self.assertEqual(alpha.getpixel((256, 256)), 255)
            info.assert_called_once_with("视频贴纸转码完成，耗时 %.2f 秒", 1.25)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_tgs_sticker_uses_bundled_converter_in_project_container(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(
            filename="sticker.tgs",
            media_type="application/x-tgsticker",
        )
        media.write(gzip.compress(b'{"v":"5.7.4","fr":30}'))
        commands: list[tuple[str, ...]] = []

        async def create_process(*args: str, **kwargs):
            commands.append(args)
            output = Path(args[args.index("--output") + 1])
            output.write_bytes(b"GIF89aresult")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"", b"")),
            )

        try:
            with (
                patch("src.sticker.asyncio.create_subprocess_exec", side_effect=create_process),
                patch(
                    "src.sticker.CONTAINER_MARKER",
                    SimpleNamespace(is_file=Mock(return_value=True)),
                ),
                patch(
                    "src.sticker.time",
                    SimpleNamespace(monotonic=Mock(side_effect=[40.0, 41.5])),
                ),
                patch("src.sticker.baselog.info") as info,
            ):
                await tgs_sticker_to_gif(media)
            self.assertEqual(media.filename, "sticker.gif")
            self.assertEqual(media.media_type, "image/gif")
            self.assertEqual(media.file.read(6), b"GIF89a")
            self.assertEqual(commands[0][:2], ("bash", "/usr/local/bin/lottie_to_gif.sh"))
            self.assertIn("--fps", commands[0])
            self.assertEqual(len(commands), 1)
            info.assert_called_once_with("TGS 贴纸转码完成，耗时 %.2f 秒", 1.5)
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)

    async def test_tgs_sticker_uses_docker_outside_project_container(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(
            filename="sticker.tgs",
            media_type="application/x-tgsticker",
        )
        media.write(gzip.compress(b'{"v":"5.7.4","fr":30}'))
        commands: list[tuple[str, ...]] = []

        async def create_process(*args: str, **kwargs):
            commands.append(args)
            if args[:2] == ("docker", "run"):
                output = Path(args[args.index("--output") + 1]).name
                host_mount = Path(args[args.index("--volume") + 1].partition(":")[0])
                (host_mount / output).write_bytes(b"GIF89adocker")
            return SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"", b"")),
            )

        try:
            with (
                patch("src.sticker.asyncio.create_subprocess_exec", side_effect=create_process),
                patch(
                    "src.sticker.CONTAINER_MARKER",
                    SimpleNamespace(is_file=Mock(return_value=False)),
                ),
                patch("src.sticker.os.getuid", return_value=1000),
                patch("src.sticker.os.getgid", return_value=1001),
            ):
                await tgs_sticker_to_gif(media)
            self.assertEqual(commands[0][:2], ("docker", "info"))
            self.assertEqual(commands[1][:3], ("docker", "run", "--rm"))
            self.assertIn("1000:1001", commands[1])
            self.assertIn("edasriyan/lottie-to-gif@sha256:", " ".join(commands[1]))
            self.assertEqual(media.file.read(6), b"GIF89a")
        finally:
            media.close()
        self.assertEqual(media_item_budget.used, initial_items)


if __name__ == "__main__":
    unittest.main()
