from __future__ import annotations

"""有界、单并发的媒体预处理队列。"""

from asyncio import Queue, QueueEmpty, QueueFull
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.log import baselog

MEDIA_PROCESSING_QUEUE_SIZE = 16


@dataclass(slots=True, kw_only=True)
class ProcessingTask:
    """通用预处理工作及失败时的资源清理回调。"""

    run: Callable[[], Awaitable[None]]
    cleanup: Callable[[], Awaitable[None]]
    on_error: Callable[[Exception], Awaitable[None]] | None = None
    label: str = "processing-task"


class MediaProcessor:
    """串行执行 Pillow/ffmpeg 媒体工作，避免多个转换任务同时抢占资源。"""

    def __init__(self, maxsize: int = MEDIA_PROCESSING_QUEUE_SIZE) -> None:
        self.queue: Queue[ProcessingTask] = Queue(maxsize=maxsize)

    def submit(self, task: ProcessingTask) -> bool:
        """立即提交任务；队列饱和时返回 False，不阻塞 Update handler。"""
        try:
            self.queue.put_nowait(task)
        except QueueFull:
            return False
        return True

    async def run(self) -> None:
        """持续处理任务；失败或取消时清理由任务仍持有的资源。"""
        while True:
            task = await self.queue.get()
            transferred = False
            try:
                await task.run()
                transferred = True
            except Exception as error:  # noqa: BLE001
                baselog.exception("媒体预处理失败: %s", task.label)
                if task.on_error is not None:
                    try:
                        await task.on_error(error)
                    except Exception:  # noqa: BLE001
                        baselog.exception("媒体预处理失败提示发送失败: %s", task.label)
            finally:
                if not transferred:
                    await self._cleanup(task)
                self.queue.task_done()

    async def close(self) -> None:
        """清理关闭时尚未开始的全部预处理任务。"""
        while True:
            try:
                task = self.queue.get_nowait()
            except QueueEmpty:
                return
            try:
                await self._cleanup(task)
            finally:
                self.queue.task_done()

    async def _cleanup(self, task: ProcessingTask) -> None:
        try:
            await task.cleanup()
        except Exception:  # noqa: BLE001
            baselog.exception("媒体预处理资源清理失败: %s", task.label)


media_processor = MediaProcessor()
