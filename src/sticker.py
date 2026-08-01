"""将 Telegram 原生贴纸转换为 Onebot 可显示的图片。"""

import asyncio
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, UnidentifiedImageError

from src.log import baselog
from src.media import STREAM_CHUNK_SIZE, MediaFile
from src.paths import ensure_temp_dir

STICKER_TRANSCODE_TIMEOUT = 120


async def static_sticker_to_png(media: MediaFile, *, size_limit: int) -> None:
    """使用 Pillow 将静态贴纸或 TGS 缩略图转换为透明 PNG。"""
    started_at = time.monotonic()
    with NamedTemporaryFile(
        suffix=".png",
        delete=False,
        dir=str(ensure_temp_dir()),
    ) as output:
        output_path = Path(output.name)
    try:
        try:
            await asyncio.to_thread(_render_png, media, output_path)
        except (OSError, UnidentifiedImageError):
            raise ValueError("Telegram 静态贴纸转换失败") from None
        if output_path.stat().st_size > size_limit:
            raise ValueError("Telegram 贴纸转换后超过 20 MB 上限")
        await asyncio.to_thread(_replace_media_content, media, output_path)
        media.filename = f"{Path(media.filename).stem or 'sticker'}.png"
        media.media_type = "image/png"
        baselog.info("静态贴纸转换完成，耗时 %.2f 秒", time.monotonic() - started_at)
    finally:
        output_path.unlink(missing_ok=True)


async def video_sticker_to_gif(media: MediaFile, *, size_limit: int) -> None:
    """使用 ffmpeg 将 WebM/VP9 视频贴纸转换为循环透明 GIF。"""
    started_at = time.monotonic()
    with NamedTemporaryFile(
        suffix=".gif",
        delete=False,
        dir=str(ensure_temp_dir()),
    ) as output:
        output_path = Path(output.name)
    try:
        input_fd = media.file.fileno()
        media.rewind()
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                f"/proc/self/fd/{input_fd}",
                "-filter_complex",
                (
                    "[0:v]fps=15,scale=512:512:force_original_aspect_ratio=decrease:"
                    "flags=lanczos,split[s0][s1];"
                    "[s0]palettegen=reserve_transparent=1:stats_mode=diff[p];"
                    "[s1][p]paletteuse=dither=sierra2_4a:alpha_threshold=128"
                ),
                "-loop",
                "0",
                "-fs",
                str(size_limit + 1),
                str(output_path),
                pass_fds=(input_fd,),
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ValueError("视频贴纸转发需要安装 ffmpeg") from None
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=STICKER_TRANSCODE_TIMEOUT,
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"Telegram 视频贴纸转换失败: {error[-500:] or '未知错误'}")
        if output_path.stat().st_size > size_limit:
            raise ValueError("Telegram 贴纸转换后超过 20 MB 上限")
        await asyncio.to_thread(_replace_media_content, media, output_path)
        media.filename = f"{Path(media.filename).stem or 'sticker'}.gif"
        media.media_type = "image/gif"
        baselog.info("视频贴纸转码完成，耗时 %.2f 秒", time.monotonic() - started_at)
    finally:
        output_path.unlink(missing_ok=True)


def _render_png(media: MediaFile, output_path: Path) -> None:
    media.rewind()
    with Image.open(media.file) as image:
        image.load()
        image.convert("RGBA").save(output_path, format="PNG", optimize=True)


def _replace_media_content(media: MediaFile, output_path: Path) -> None:
    media.file.seek(0)
    media.file.truncate()
    media.size = 0
    with output_path.open("rb") as converted:
        while chunk := converted.read(STREAM_CHUNK_SIZE):
            media.write(chunk)
    media.rewind()
