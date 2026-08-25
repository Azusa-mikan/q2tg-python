import asyncio

import pytest

from src.lifecycle import await_cancelled, await_completion_on_cancel


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


@pytest.mark.asyncio
async def test_cancelled_task_failure_is_logged_and_does_not_abort_cleanup() -> None:
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    task = asyncio.create_task(_raise_cleanup_error())
    with pytest.MonkeyPatch.context() as monkeypatch:
        logged = []
        monkeypatch.setattr(
            "src.lifecycle.baselog.exception",
            lambda *args, **kwargs: logged.append((args, kwargs)),
        )
        await await_cancelled(task, log_label="测试清理任务异常")

    assert logged == [(('%s', "测试清理任务异常"), {})]


async def _raise_cleanup_error() -> None:
    raise RuntimeError("cleanup failed")
