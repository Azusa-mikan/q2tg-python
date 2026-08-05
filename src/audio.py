"""把 OneBot 语音规范化为 Telegram 可播放的 Ogg/Opus。"""

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from src.log import baselog
from src.media import (
    FFMPEG_BASE_ARGS,
    TELEGRAM_UPLOAD_LIMIT,
    TELEGRAM_UPLOAD_LIMIT_TEXT,
    MediaFile,
    communicate_media_process,
    decode_process_error,
    finalize_media,
    start_media_process,
    transcode_target,
)
from src.messages import MediaTooLargeError
from src.runtime_events import emit_runtime_event

SILK_SAMPLE_RATE = 24000
RECORD_SIZE_LIMIT = TELEGRAM_UPLOAD_LIMIT
PROBE_TIMEOUT = 30
TRANSCODE_TIMEOUT = 120
SILK_HEADERS = (b"#!SILK_V3", b"\x02#!SILK_V3")

_PILK_WORKER = f"""
import resource
import sys
import pilk

limit = int(sys.argv[3])
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
pilk.decode(sys.argv[1], sys.argv[2], pcm_rate={SILK_SAMPLE_RATE})
"""

_EXEC_WITH_FILE_LIMIT = """
import os
import resource
import sys

limit = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
os.execvp(sys.argv[2], sys.argv[2:])
"""


async def normalize_onebot_record(media: MediaFile) -> None:
    """识别 OneBot 语音格式，并原地规范化为不超过 50 MB 的 Ogg/Opus。"""
    started_at = time.monotonic()
    async with transcode_target(".pcm") as pcm_path, transcode_target(".ogg") as output_path:
        if _is_silk(media):
            await _decode_silk(media, pcm_path)
            await _transcode_to_ogg(
                input_path=str(pcm_path),
                output_path=output_path,
                input_args=("-f", "s16le", "-ar", str(SILK_SAMPLE_RATE), "-ac", "1"),
            )
        else:
            codec, format_name = await _probe_audio(media)
            if codec == "opus" and "ogg" in format_name.split(","):
                media.filename = f"{Path(media.filename).stem or 'voice'}.ogg"
                media.media_type = "audio/ogg"
                media.rewind()
                emit_runtime_event("capability.succeeded", "onebot.voice.compatible")
                return
            input_fd = media.fileno()
            media.rewind()
            await _transcode_to_ogg(
                input_path=f"/proc/self/fd/{input_fd}",
                output_path=output_path,
                pass_fds=(input_fd,),
            )

        output_size = output_path.stat().st_size
        if output_size > RECORD_SIZE_LIMIT:
            raise MediaTooLargeError(
                f"OneBot 语音转码后超过 {TELEGRAM_UPLOAD_LIMIT_TEXT}，无法转发"
            )
        if output_size == 0 or not _is_ogg_file(output_path):
            raise ValueError("OneBot 语音转码未生成有效的 Ogg 文件")
        finalize_media(media, output_path, stem_fallback="voice", suffix=".ogg", media_type="audio/ogg")
        emit_runtime_event("capability.succeeded", "onebot.voice.transcoded")
        baselog.info("OneBot 语音规范化完成，耗时 %.2f 秒", time.monotonic() - started_at)


async def _decode_silk(media: MediaFile, pcm_path: Path) -> None:
    input_fd = media.fileno()
    media.rewind()
    process = await start_media_process(
        sys.executable,
        "-c",
        _PILK_WORKER,
        f"/proc/self/fd/{input_fd}",
        str(pcm_path),
        str(RECORD_SIZE_LIMIT),
        pass_fds=(input_fd,),
        missing_error="语音转发需要安装 ffmpeg 和 ffprobe",
    )
    _, stderr = await communicate_media_process(
        process,
        timeout=TRANSCODE_TIMEOUT,
        timeout_error="OneBot 语音处理超时",
    )
    pcm_size = pcm_path.stat().st_size
    if process.returncode == -signal.SIGXFSZ or (
        process.returncode != 0 and pcm_size >= RECORD_SIZE_LIMIT
    ):
        raise MediaTooLargeError(
            f"OneBot SILK 解码后超过 {TELEGRAM_UPLOAD_LIMIT_TEXT}，无法转发"
        )
    if process.returncode != 0:
        raise ValueError(f"OneBot SILK 语音解码失败: {decode_process_error(stderr)}")


async def _probe_audio(media: MediaFile) -> tuple[str, str]:
    input_fd = media.fileno()
    media.rewind()
    process = await start_media_process(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=format_name:stream=codec_type,codec_name",
        "-of",
        "json",
        f"/proc/self/fd/{input_fd}",
        pass_fds=(input_fd,),
        stdout=asyncio.subprocess.PIPE,
        missing_error="语音转发需要安装 ffmpeg 和 ffprobe",
    )
    stdout, stderr = await communicate_media_process(
        process,
        timeout=PROBE_TIMEOUT,
        timeout_error="OneBot 语音处理超时",
    )
    if process.returncode != 0:
        raise ValueError(f"无法识别 OneBot 语音格式: {decode_process_error(stderr)}")
    try:
        result = json.loads(stdout)
        codec = next(
            stream["codec_name"]
            for stream in result.get("streams", [])
            if stream.get("codec_type") == "audio" and isinstance(stream.get("codec_name"), str)
        )
        format_name = result["format"]["format_name"]
        if not isinstance(format_name, str):
            raise KeyError
    except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
        raise ValueError("OneBot 语音中没有可用的音频流") from None
    return codec, format_name


async def _transcode_to_ogg(
    *,
    input_path: str,
    output_path: Path,
    input_args: tuple[str, ...] = (),
    pass_fds: tuple[int, ...] = (),
) -> None:
    ffmpeg_args = (
        *FFMPEG_BASE_ARGS,
        *input_args,
        "-i",
        input_path,
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-application",
        "voip",
        "-f",
        "ogg",
        str(output_path),
    )
    process = await start_media_process(
        sys.executable,
        "-c",
        _EXEC_WITH_FILE_LIMIT,
        str(RECORD_SIZE_LIMIT),
        *ffmpeg_args,
        pass_fds=pass_fds,
        missing_error="语音转发需要安装 ffmpeg 和 ffprobe",
    )
    _, stderr = await communicate_media_process(
        process,
        timeout=TRANSCODE_TIMEOUT,
        timeout_error="OneBot 语音处理超时",
    )
    output_size = output_path.stat().st_size
    if process.returncode == -signal.SIGXFSZ or (
        process.returncode != 0 and output_size >= RECORD_SIZE_LIMIT
    ):
        raise MediaTooLargeError(
            f"OneBot 语音转码后超过 {TELEGRAM_UPLOAD_LIMIT_TEXT}，无法转发"
        )
    if process.returncode != 0:
        raise ValueError(f"OneBot 语音转码失败: {decode_process_error(stderr)}")


def _is_silk(media: MediaFile) -> bool:
    position = media.file.tell()
    try:
        media.file.seek(0)
        header = media.file.read(10)
        return any(header.startswith(candidate) for candidate in SILK_HEADERS)
    finally:
        media.file.seek(position)


def _is_ogg_file(path: Path) -> bool:
    with path.open("rb") as converted:
        return converted.read(4) == b"OggS"
