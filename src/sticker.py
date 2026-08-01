"""将 Telegram 原生贴纸转换为 Onebot 可显示的图片。"""

import asyncio
import gzip
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from PIL import Image, UnidentifiedImageError

from src.log import baselog
from src.media import STREAM_CHUNK_SIZE, MediaFile
from src.paths import ensure_temp_dir

STICKER_TRANSCODE_TIMEOUT = 120
TGS_JSON_SIZE_LIMIT = 5_000_000
CONTAINER_MARKER = Path("/app/.q2tg-container")
LOTTIE_CONVERTER_IMAGE = (
    "edasriyan/lottie-to-gif@"
    "sha256:0eb24cf4f38c6c62b66f37bfba463fff4de4f64cb9a6127df0b9543fc4b9c649"
)


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
                "-c:v",
                "libvpx-vp9",
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


async def tgs_sticker_to_gif(media: MediaFile) -> None:
    """使用 lottie-converter 将 TGS 转换为循环透明 GIF。"""
    started_at = time.monotonic()
    temp_dir = str(ensure_temp_dir())
    with TemporaryDirectory(dir=temp_dir) as conversion_dir:
        source_path = Path(conversion_dir) / "sticker.json"
        output_path = Path(conversion_dir) / "sticker.gif"
        try:
            media.rewind()
            with gzip.GzipFile(fileobj=media.file, mode="rb") as compressed:
                _copy_tgs_json(compressed, source_path)
        except (gzip.BadGzipFile, EOFError, OSError):
            raise ValueError("Telegram TGS 贴纸解压失败") from None
        finally:
            media.rewind()

        if CONTAINER_MARKER.is_file():
            await _run_sticker_process(
                "bash",
                "/usr/local/bin/lottie_to_gif.sh",
                *_lottie_arguments(source_path, output_path),
                missing_error="TGS 贴纸转发需要安装 bash 和 lottie-converter",
                failure_prefix="Telegram TGS 贴纸转换失败",
            )
        else:
            await _run_sticker_process(
                "docker",
                "info",
                "--format",
                "{{.ServerVersion}}",
                missing_error="本地 TGS 贴纸转发需要安装 Docker",
                failure_prefix="Docker daemon 不可用或当前用户无访问权限",
            )
            mount = source_path.parent.resolve()
            await _run_sticker_process(
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--volume",
                f"{mount}:/source",
                LOTTIE_CONVERTER_IMAGE,
                "bash",
                "/usr/bin/lottie_to_gif.sh",
                *_lottie_arguments(
                    Path("/source") / source_path.name,
                    Path("/source") / output_path.name,
                ),
                missing_error="本地 TGS 贴纸转发需要安装 Docker",
                failure_prefix="Docker TGS 贴纸转换失败",
            )
        await asyncio.to_thread(_replace_media_content, media, output_path)
        media.filename = f"{Path(media.filename).stem or 'sticker'}.gif"
        media.media_type = "image/gif"
        baselog.info("TGS 贴纸转码完成，耗时 %.2f 秒", time.monotonic() - started_at)


def _lottie_arguments(source_path: Path, output_path: Path) -> tuple[str, ...]:
    return (
        "--width",
        "512",
        "--height",
        "512",
        "--fps",
        "30",
        "--quality",
        "90",
        "--threads",
        "1",
        "--output",
        str(output_path),
        str(source_path),
    )


def _copy_tgs_json(compressed: gzip.GzipFile, output_path: Path) -> None:
    """限制解压体积，避免畸形 TGS 过度占用临时存储。"""
    written = 0
    with output_path.open("wb") as output:
        while chunk := compressed.read(STREAM_CHUNK_SIZE):
            written += len(chunk)
            if written > TGS_JSON_SIZE_LIMIT:
                raise ValueError("Telegram TGS 贴纸解压后过大")
            output.write(chunk)


async def _run_sticker_process(
    *args: str,
    missing_error: str,
    failure_prefix: str,
) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ValueError(missing_error) from None
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
        raise ValueError(f"{failure_prefix}: {error[-500:] or '未知错误'}")


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
