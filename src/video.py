"""把 Telegram 视频规范化为 Onebot 客户端普遍可播放的 MP4。"""

import asyncio
import json
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.log import baselog
from src.media import STREAM_CHUNK_SIZE, MediaFile

PROBE_TIMEOUT = 30
TRANSCODE_TIMEOUT = 300


async def normalize_video_for_onebot(media: MediaFile, *, size_limit: int) -> None:
    """按需将视频原地替换为 H.264 + AAC MP4。

    Telegram 接受 HEVC MP4，但部分 Onebot 客户端播放时会黑屏；如果客户端显示
    “视频已过期”，应升级 SnowLuma。
    已兼容的 H.264/AAC 文件保持原样；其它编码通过 ffmpeg 转码。转码结果先写入
    独立临时路径，成功且未超限后再覆盖原 MediaFile，异常时原文件保持可清理。
    """
    video_codec, audio_codecs = await _probe_codecs(media)
    if video_codec == "h264" and all(codec == "aac" for codec in audio_codecs):
        return

    started_at = time.monotonic()
    with NamedTemporaryFile(suffix=".mp4", delete=False) as output:
        output_path = Path(output.name)
    try:
        input_fd = media.file.fileno()
        media.rewind()
        process = await _start_process(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            f"/proc/self/fd/{input_fd}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-fs",
            str(size_limit + 1),
            str(output_path),
            pass_fds=(input_fd,),
        )
        _, stderr = await _communicate(process, timeout=TRANSCODE_TIMEOUT)
        if process.returncode != 0:
            raise ValueError(f"Telegram 视频转码失败: {_safe_error(stderr)}")
        if output_path.stat().st_size > size_limit:
            raise ValueError("Telegram 视频转码后超过 20 MB 上限")

        await asyncio.to_thread(_replace_media_content, media, output_path)
        media.filename = f"{Path(media.filename).stem or 'video'}.mp4"
        media.media_type = "video/mp4"
        baselog.info("视频转码完成，耗时 %.2f 秒", time.monotonic() - started_at)
    finally:
        output_path.unlink(missing_ok=True)


async def _probe_codecs(media: MediaFile) -> tuple[str | None, tuple[str, ...]]:
    input_fd = media.file.fileno()
    media.rewind()
    process = await _start_process(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of",
        "json",
        f"/proc/self/fd/{input_fd}",
        pass_fds=(input_fd,),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(process, timeout=PROBE_TIMEOUT)
    if process.returncode != 0:
        raise ValueError(f"Telegram 视频格式检测失败: {_safe_error(stderr)}")
    try:
        streams = json.loads(stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError):
        raise ValueError("Telegram 视频格式检测失败") from None
    video_codec: str | None = next(
        (
            stream.get("codec_name")
            for stream in streams
            if stream.get("codec_type") == "video"
            and isinstance(stream.get("codec_name"), str)
        ),
        None,
    )
    audio_codecs = tuple(
        stream.get("codec_name")
        for stream in streams
        if stream.get("codec_type") == "audio" and isinstance(stream.get("codec_name"), str)
    )
    if video_codec is None:
        raise ValueError("Telegram 视频中没有可用的视频流")
    return video_codec, audio_codecs


async def _start_process(*args: str, **kwargs) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            *args,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError:
        raise ValueError("视频转发需要安装 ffmpeg 和 ffprobe") from None


async def _communicate(
    process: asyncio.subprocess.Process,
    *,
    timeout: int,
) -> tuple[bytes, bytes]:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return stdout or b"", stderr or b""


def _replace_media_content(media: MediaFile, output_path: Path) -> None:
    """在工作线程中把转码结果分块覆盖回 spool。"""
    media.file.seek(0)
    media.file.truncate()
    media.size = 0
    with output_path.open("rb") as converted:
        while chunk := converted.read(STREAM_CHUNK_SIZE):
            media.write(chunk)
    media.rewind()


def _safe_error(stderr: bytes) -> str:
    error = stderr.decode("utf-8", errors="replace").strip()
    return error[-500:] if error else "未知错误"
