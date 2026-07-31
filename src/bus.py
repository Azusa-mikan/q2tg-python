from __future__ import annotations

"""按目标平台隔离的发送队列和独立重试调度队列。"""

import asyncio
from asyncio import Queue, QueueFull
from dataclasses import dataclass

from src.log import baselog
from src.messages import FailureAction, SendTarget, SendTask


@dataclass(frozen=True, slots=True)
class Shutdown:
    """单个目标队列消费者的停止信号。"""


SHUTDOWN = Shutdown()
QueueItem = SendTask | Shutdown


class MessageBus:
    """管理 Onebot、Telegram 发送队列以及独立的重试队列。

    目标队列只关心任务发往哪里，不关心消息来源或业务 DTO。失败分类、最大次数、
    耗尽处理和资源释放全部由 SendTask 携带，因此后续语音、文件等类型无需修改总线。
    """

    def __init__(self, maxsize: int = 100) -> None:
        self.onebot_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.telegram_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        # 重试队列是目标队列之间的内部调度通道，不接收外部流量。保持无界可避免
        # “目标队列满、worker 等重试槽、dispatcher 又等目标槽”的循环背压。
        self.retry_queue: Queue[SendTask | Shutdown] = Queue()

    async def put(self, task: SendTask) -> None:
        """按任务目标路由，并对异步生产者施加背压。"""
        await self._target_queue(task.target).put(task)

    def put_nowait(self, task: SendTask) -> bool:
        """按任务目标立即入队；满队列时返回 False。"""
        try:
            self._target_queue(task.target).put_nowait(task)
        except QueueFull:
            return False
        return True

    async def stop_consumer(self, target: SendTarget) -> None:
        """把停止信号放到指定目标队列尾部。"""
        await self._target_queue(target).put(SHUTDOWN)

    async def stop_retry_dispatcher(self) -> None:
        """在应用关闭时停止重试调度器。"""
        await self.retry_queue.put(SHUTDOWN)

    async def join(self, target: SendTarget) -> None:
        """等待目标队列及其可能产生的重试任务稳定排空。"""
        queue = self._target_queue(target)
        while True:
            await queue.join()
            await self.retry_queue.join()
            await queue.join()
            # task_done 与下一次 retry put 之间不存在 await，但调度器把 retry
            # 重新投递目标队列后仍需给消费者一个运行机会再判断稳定状态。
            await asyncio.sleep(0)
            if queue.empty() and self.retry_queue.empty():
                return

    async def dispatch_retries(self) -> None:
        """把失败任务从独立重试队列重新投递到其目标队列尾部。"""
        while True:
            item = await self.retry_queue.get()
            try:
                if isinstance(item, Shutdown):
                    return
                await self.put(item)
            finally:
                self.retry_queue.task_done()

    async def consume(self, target: SendTarget) -> None:
        """消费一个目标平台的通用任务；DEFER 会保留任务并结束当前消费者。"""
        queue = self._target_queue(target)
        while True:
            item = await queue.get()
            requeued = False
            try:
                if isinstance(item, Shutdown):
                    return
                if not isinstance(item, SendTask):
                    raise TypeError(f"未知队列项: {type(item)!r}")
                try:
                    await item.send()
                except Exception as error:  # noqa: BLE001
                    action = item.failure_action(error)
                    if action is FailureAction.DEFER:
                        requeued = True
                        self.retry_queue.put_nowait(item)
                        return
                    if action is FailureAction.RETRY:
                        item.failures += 1
                        if item.failures < item.max_attempts:
                            requeued = True
                            self.retry_queue.put_nowait(item)
                        else:
                            await self._run_failed(item, error, exhausted=True)
                    else:
                        baselog.exception("发送任务失败，不重试: %s", item.label)
                        await self._run_failed(item, error, exhausted=False)
                except asyncio.CancelledError:
                    # 连接关闭可能取消正在等待 RPC 的 Onebot 任务；保留任务给下次连接。
                    requeued = True
                    self.retry_queue.put_nowait(item)
                    raise
            finally:
                if isinstance(item, SendTask) and not requeued:
                    await self._finalize(item)
                queue.task_done()

    async def _run_failed(
        self,
        task: SendTask,
        error: Exception,
        *,
        exhausted: bool,
    ) -> None:
        if exhausted:
            baselog.exception(
                "发送任务连续 %s 次失败: %s",
                task.max_attempts,
                task.label,
            )
        if task.on_failed is None:
            return
        try:
            await task.on_failed(error)
        except Exception:  # noqa: BLE001
            baselog.exception("发送任务最终失败后的处理失败: %s", task.label)

    async def _finalize(self, task: SendTask) -> None:
        if task.finalize is None:
            return
        try:
            await task.finalize()
        except Exception:  # noqa: BLE001
            baselog.exception("发送任务资源清理失败: %s", task.label)

    def _target_queue(self, target: SendTarget) -> Queue[QueueItem]:
        if target is SendTarget.ONEBOT:
            return self.onebot_queue
        if target is SendTarget.TELEGRAM:
            return self.telegram_queue
        raise ValueError(f"未知发送目标: {target!r}")


message_bus = MessageBus()
