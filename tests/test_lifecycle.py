import asyncio

import pytest

from src.lifecycle import await_completion_on_cancel


@pytest.mark.asyncio
async def test_critical_operation_finishes_before_cancellation_propagates() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def operation() -> None:
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(await_completion_on_cancel(operation()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
