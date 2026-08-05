"""关停路径共用的任务回收工具。"""

import asyncio
from collections.abc import Awaitable

from src.log import baselog


async def await_completion_on_cancel[T](operation: Awaitable[T]) -> T:
    """外层取消时等待关键操作结束，再传播首次取消。"""
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException as error:
            cancelled.add_note(f"Cleanup operation failed: {error!r}")
        raise


async def await_cancelled(task: asyncio.Task[object], *, log_label: str | None = None) -> None:
    """等待一个已被 cancel 的任务结束并处理其退出结果。

    调用方应先对全部任务发出 cancel，再逐个 await，使它们并行收尾。
    CancelledError 是预期的取消结果，直接吞掉。其它异常表示任务在取消前已经
    失败：给出 log_label 时记录后继续，使调用方的后续资源关闭不被跳过；未给出
    log_label 时向上抛出，交由调用方处理。
    """
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        if log_label is None:
            raise
        baselog.exception("%s", log_label)
