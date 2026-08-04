import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.bus import MessageBus
from src.messages import SendTarget, SendTask
from src.processing import MediaProcessor, ProcessingTask


@pytest.mark.asyncio
class TestMediaProcessor:
    async def test_submit_is_bounded_and_non_blocking(self) -> None:
        processor = MediaProcessor(maxsize=1)
        first_cleanup = AsyncMock()
        second_cleanup = AsyncMock()
        first = ProcessingTask(run=AsyncMock(), cleanup=first_cleanup)
        second = ProcessingTask(run=AsyncMock(), cleanup=second_cleanup)

        assert processor.submit(first)
        assert not processor.submit(second)

        await processor.close()
        first_cleanup.assert_awaited_once_with()
        second_cleanup.assert_not_awaited()

    async def test_single_worker_never_runs_two_tasks_concurrently(self) -> None:
        processor = MediaProcessor()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def run_first() -> None:
            first_started.set()
            await release_first.wait()

        async def run_second() -> None:
            second_started.set()

        first_cleanup = AsyncMock()
        second_cleanup = AsyncMock()
        first = ProcessingTask(run=run_first, cleanup=first_cleanup)
        second = ProcessingTask(run=run_second, cleanup=second_cleanup)
        processor.submit(first)
        processor.submit(second)
        worker = asyncio.create_task(processor.run())
        try:
            await first_started.wait()
            await asyncio.sleep(0)
            assert not second_started.is_set()
            release_first.set()
            await processor.queue.join()
            assert second_started.is_set()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        first_cleanup.assert_not_awaited()
        second_cleanup.assert_not_awaited()

    async def test_failure_cleans_resources_and_reports_error(self) -> None:
        processor = MediaProcessor()
        error = RuntimeError("failed")
        cleanup = AsyncMock()
        on_error = AsyncMock()
        task = ProcessingTask(
            run=AsyncMock(side_effect=error),
            cleanup=cleanup,
            on_error=on_error,
        )
        processor.submit(task)
        with patch("src.processing.baselog.exception"):
            worker = asyncio.create_task(processor.run())
            try:
                await processor.queue.join()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        cleanup.assert_awaited_once_with()
        on_error.assert_awaited_once_with(error)

    async def test_regular_send_task_can_overtake_processing(self) -> None:
        processor = MediaProcessor()
        bus = MessageBus()
        processing_started = asyncio.Event()
        release_processing = asyncio.Event()
        send = AsyncMock()

        async def process_video() -> None:
            processing_started.set()
            await release_processing.wait()

        processor.submit(
            ProcessingTask(run=process_video, cleanup=AsyncMock())
        )
        media_worker = asyncio.create_task(processor.run())
        send_worker = asyncio.create_task(bus.consume(SendTarget.ONEBOT))
        try:
            await processing_started.wait()
            await bus.put(SendTask(target=SendTarget.ONEBOT, send=send))
            await bus.join(SendTarget.ONEBOT)
            send.assert_awaited_once_with()
            assert not release_processing.is_set()
        finally:
            release_processing.set()
            await processor.queue.join()
            media_worker.cancel()
            send_worker.cancel()
            await asyncio.gather(
                media_worker,
                send_worker,
                return_exceptions=True,
            )
