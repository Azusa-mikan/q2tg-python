import asyncio
import threading
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telegram import InputFile

from src.media import (
    MEDIA_MEMORY_TIER_LIMIT,
    SPOOL_MEMORY_LIMIT,
    ByteBudget,
    MediaFile,
    MemoryBudget,
    communicate_media_process,
    media_item_budget,
    media_memory_budget,
    replace_media_content,
    run_media_thread,
)


@pytest.mark.asyncio
class TestMediaFile:
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
                    assert temp_dir.is_dir()
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
            assert cast(Any, media.file)._rolled
            assert media.size == SPOOL_MEMORY_LIMIT + 1
        finally:
            media.close()
        assert media_item_budget.used == initial_items

    async def test_unknown_expected_size_keeps_legacy_memory_path(self) -> None:
        # 没有 Content-Length 时不预留额度，行为与分档前完全一致。
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        try:
            media.write(b"x" * 512)
            assert not cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_middle_tier_stays_in_memory_with_budget(self) -> None:
        expected = 3 * 1024 * 1024
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(
            filename="image.jpg",
            media_type="image/jpeg",
            expected_size=expected,
        )
        try:
            media.write(b"x" * (SPOOL_MEMORY_LIMIT + 1))
            # 1 MiB 以上仍留在内存，代价是占用一笔内存额度。
            assert not cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory + expected
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_large_expected_size_spools_to_disk_immediately(self) -> None:
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(
            filename="video.mp4",
            media_type="video/mp4",
            expected_size=MEDIA_MEMORY_TIER_LIMIT + 1,
        )
        try:
            # max_size=0 在 SpooledTemporaryFile 里表示永不 rollover，因此大文件
            # 必须在创建时显式落盘，不能依赖阈值触发。
            assert cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_middle_tier_degrades_to_disk_when_budget_exhausted(self) -> None:
        expected = 3 * 1024 * 1024
        initial_memory = media_memory_budget.used
        with patch("src.media.media_memory_budget", MemoryBudget(expected)):
            hog = await MediaFile.create(filename="a.jpg", expected_size=expected)
            try:
                degraded = await MediaFile.create(filename="b.jpg", expected_size=expected)
                try:
                    # 额度用尽时降级落盘，而不是报错或等待。
                    assert cast(Any, degraded.file)._rolled
                    assert not cast(Any, hog.file)._rolled
                finally:
                    degraded.close()
            finally:
                hog.close()
        assert media_memory_budget.used == initial_memory

    async def test_understated_expected_size_rolls_over_and_releases(self) -> None:
        initial_memory = media_memory_budget.used
        declared = 2 * 1024 * 1024
        media = await MediaFile.create(
            filename="image.jpg",
            media_type="image/jpeg",
            expected_size=declared,
        )
        try:
            # 额度按声明值收取，因此驻留内存的上限也是声明值本身，写满仍在内存。
            media.write(b"x" * declared)
            assert not cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory + declared
            # 声明值偏小时实际写入会突破额度，此时必须落盘并归还额度。
            media.write(b"x")
            assert cast(Any, media.file)._rolled
            assert media.size == declared + 1
            assert media_memory_budget.used == initial_memory
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_transcode_output_leaves_memory_tier(self) -> None:
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(
            filename="voice.silk",
            media_type="audio/silk",
            expected_size=3 * 1024 * 1024,
        )
        try:
            media.write(b"original")
            assert media_memory_budget.used > initial_memory
            with TemporaryDirectory() as directory:
                output_path = Path(directory) / "converted.ogg"
                output_path.write_bytes(b"converted-payload")
                await asyncio.to_thread(replace_media_content, media, output_path)
            # 转码产物大小与声明值无关，一律退出内存档。
            assert cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory
            assert media.size == len(b"converted-payload")
            media.rewind()
            assert media.file.read() == b"converted-payload"
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_fileno_leaves_memory_tier(self) -> None:
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(
            filename="voice.silk",
            media_type="audio/silk",
            expected_size=3 * 1024 * 1024,
        )
        try:
            media.write(b"payload")
            assert media_memory_budget.used > initial_memory
            # 子进程需要真实 fd，取 fd 必然落盘，所以额度要在此归还。
            assert media.fileno() > 0
            assert cast(Any, media.file)._rolled
            assert media_memory_budget.used == initial_memory
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_rollover_failure_releases_memory_budget_on_close(self) -> None:
        initial_memory = media_memory_budget.used
        media = await MediaFile.create(
            filename="image.jpg",
            media_type="image/jpeg",
            expected_size=3 * 1024 * 1024,
        )
        reserved = media._memory_reserved
        try:
            with (
                patch.object(media.file, "rollover", side_effect=OSError("disk full")),
                pytest.raises(OSError, match="disk full"),
            ):
                media.leave_memory_tier()
            assert media._memory_reserved == reserved
        finally:
            media.close()
        assert media_memory_budget.used == initial_memory

    async def test_cancelled_media_thread_finishes_before_propagating(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def work() -> None:
            started.set()
            release.wait()
            finished.set()

        operation = asyncio.create_task(run_media_thread(work))
        while not started.is_set():
            await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished.is_set()

    async def test_memory_budget_never_blocks(self) -> None:
        budget = MemoryBudget(10)
        assert budget.try_acquire(10) is True
        # 语义是“拿不到就落盘”，因此额度不足时立即返回 False，不等待。
        assert budget.try_acquire(1) is False
        assert budget.used == 10
        budget.release(10)
        assert budget.used == 0

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
            assert upload.input_file_content is media.file
        finally:
            media.close()

    async def test_close_waits_for_active_stream(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        media.write(b"image")
        stream = media.chunks()
        assert await anext(stream) == b"image"

        media.close()
        assert media_item_budget.used == initial_items + 1
        await stream.aclose()
        assert media_item_budget.used == initial_items

    async def test_unstarted_stream_can_be_closed(self) -> None:
        initial_items = media_item_budget.used
        media = await MediaFile.create(filename="image.jpg", media_type="image/jpeg")
        stream = media.chunks()
        media.close()
        assert media_item_budget.used == initial_items + 1

        await stream.aclose()
        assert media_item_budget.used == initial_items

    async def test_byte_budget_blocks_until_release(self) -> None:
        budget = ByteBudget(10)
        await budget.acquire(10)
        waiter = asyncio.create_task(budget.acquire(1))
        await asyncio.sleep(0)
        assert not waiter.done()

        await budget.release(10)
        await waiter
        assert budget.used == 1
        await budget.release(1)

    async def test_media_process_timeout_kills_and_reaps_process(self) -> None:
        communication = asyncio.get_running_loop().create_future()
        process = SimpleNamespace(
            communicate=Mock(return_value=communication),
            returncode=None,
            kill=Mock(),
            wait=AsyncMock(),
        )

        with (
            patch("src.media.asyncio.wait_for", side_effect=TimeoutError),
            pytest.raises(ValueError, match="媒体处理超时"),
        ):
            await communicate_media_process(
                cast(asyncio.subprocess.Process, process),
                timeout=1,
                timeout_error="媒体处理超时",
            )

        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once_with()
        communication.cancel()
