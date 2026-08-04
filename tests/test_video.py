import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.media import MediaFile, media_item_budget
from src.video import normalize_video_for_onebot


@pytest.mark.asyncio
class TestVideoNormalization:
    async def _generate_video(self, codec: str) -> bytes:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp4"
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
                "color=c=black:s=64x64:d=0.2",
                "-an",
                "-c:v",
                codec,
                "-pix_fmt",
                "yuv420p",
                str(path),
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode(errors="replace")
            return path.read_bytes()

    async def _codec(self, media: MediaFile) -> str:
        input_fd = media.file.fileno()
        media.rewind()
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            f"/proc/self/fd/{input_fd}",
            pass_fds=(input_fd,),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode(errors="replace")
        return json.loads(stdout)["streams"][0]["codec_name"]

    async def test_h264_mp4_is_kept_without_transcoding(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="sample.mp4", media_type="video/mp4")
        original = await self._generate_video("libx264")
        media.write(original)
        media.rewind()
        try:
            with patch("src.video.baselog.info") as info:
                await normalize_video_for_onebot(media, size_limit=20_000_000)
            assert media.size == len(original)
            assert media.file.read() == original
            info.assert_not_called()
        finally:
            media.close()
        assert media_item_budget.used == initial_items

    async def test_hevc_mp4_is_transcoded_to_h264(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="sample.mp4", media_type="video/mp4")
        media.write(await self._generate_video("libx265"))
        media.rewind()
        try:
            assert await self._codec(media) == "hevc"
            with (
                patch(
                    "src.video.time",
                    SimpleNamespace(monotonic=Mock(side_effect=[10.0, 12.345])),
                ),
                patch("src.video.baselog.info") as info,
            ):
                await normalize_video_for_onebot(media, size_limit=20_000_000)
            assert await self._codec(media) == "h264"
            assert media.media_type == "video/mp4"
            assert media.filename == "sample.mp4"
            info.assert_called_once_with("视频转码完成，耗时 %.2f 秒", 2.3450000000000006)
        finally:
            media.close()
        assert media_item_budget.used == initial_items
