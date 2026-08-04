from __future__ import annotations

"""按目标平台隔离的发送队列和独立重试调度队列。"""

import asyncio
from asyncio import Queue, QueueFull
from dataclasses import dataclass

from src.log import baselog
from src.messages import FailureAction, SendLane, SendTarget, SendTask
from src.runtime_events import emit_runtime_event, runtime_work


@dataclass(frozen=True, slots=True)
class Shutdown:
    """单个目标队列消费者的停止信号。"""


SHUTDOWN = Shutdown()
QueueItem = SendTask | Shutdown


class MessageBus:
    """管理 OneBot、Telegram 发送队列以及独立的重试队列。

    目标队列只关心任务发往哪里，不关心消息来源或业务 DTO。失败分类、最大次数、
    耗尽处理和资源释放全部由 SendTask 携带，因此后续语音、文件等类型无需修改总线。
    """

    def __init__(self, maxsize: int = 100) -> None:
        self.onebot_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.telegram_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.onebot_event_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.telegram_event_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.onebot_system_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        self.telegram_system_queue: Queue[QueueItem] = Queue(maxsize=maxsize)
        # 重试队列是目标队列之间的内部调度通道，不接收外部流量。保持无界可避免
        # “目标队列满、worker 等重试槽、dispatcher 又等目标槽”的循环背压。
        self.retry_queue: Queue[SendTask | Shutdown] = Queue()

    async def put(self, task: SendTask) -> None:
        """按任务目标路由，并对异步生产者施加背压。"""
        await self._target_queue(task.target, task.lane).put(task)
        emit_runtime_event("send.enqueued", task.label)

    def put_nowait(self, task: SendTask) -> bool:
        """按任务目标立即入队；满队列时返回 False。"""
        try:
            self._target_queue(task.target, task.lane).put_nowait(task)
        except QueueFull:
            return False
        emit_runtime_event("send.enqueued", task.label)
        return True

    async def stop_consumer(
        self,
        target: SendTarget,
        lane: SendLane = SendLane.MESSAGE,
    ) -> None:
        """把停止信号放到指定目标和 lane 的队列尾部。"""
        await self._target_queue(target, lane).put(SHUTDOWN)

    async def stop_retry_dispatcher(self) -> None:
        """在应用关闭时停止重试调度器。"""
        await self.retry_queue.put(SHUTDOWN)

    async def join(
        self,
        target: SendTarget,
        lane: SendLane = SendLane.MESSAGE,
    ) -> None:
        """等待指定目标和 lane 及其可能产生的重试任务稳定排空。"""
        queue = self._target_queue(target, lane)
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

    async def consume(
        self,
        target: SendTarget,
        lane: SendLane = SendLane.MESSAGE,
    ) -> None:
        """消费一个目标平台 lane 的任务；DEFER 会保留任务并结束当前消费者。"""
        queue = self._target_queue(target, lane)
        while True:
            item = await queue.get()
            requeued = False
            sent = False
            try:
                if isinstance(item, Shutdown):
                    return
                if not isinstance(item, SendTask):
                    raise TypeError(f"未知队列项: {type(item)!r}")
                try:
                    emit_runtime_event("send.started", item.label)
                    with runtime_work(item.label):
                        await item.send()
                    sent = True
                except Exception as error:
                    action = item.failure_action(error)
                    emit_runtime_event("send.attempt_failed", item.label, error=error)
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
                            emit_runtime_event("send.failed", item.label, error=error)
                            await self._run_failed(item, error, exhausted=True)
                    else:
                        emit_runtime_event("send.failed", item.label, error=error)
                        baselog.exception("发送任务失败，不重试: %s", item.label)
                        await self._run_failed(item, error, exhausted=False)
                except asyncio.CancelledError:
                    # 连接关闭可能取消正在等待 RPC 的 OneBot 任务；保留任务给下次连接。
                    requeued = True
                    self.retry_queue.put_nowait(item)
                    emit_runtime_event("send.cancelled", item.label)
                    raise
            finally:
                if isinstance(item, SendTask) and not requeued:
                    finalized = await self._finalize(item)
                    if sent and finalized:
                        emit_runtime_event("send.succeeded", item.label)
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
            with runtime_work(task.label):
                await task.on_failed(error)
        except Exception:
            baselog.exception("发送任务最终失败后的处理失败: %s", task.label)

    async def _finalize(self, task: SendTask) -> bool:
        if task.finalize is None:
            return True
        try:
            await task.finalize()
        except Exception:
            baselog.exception("发送任务资源清理失败: %s", task.label)
            emit_runtime_event("send.finalize_failed", task.label)
            return False
        return True

    def _target_queue(
        self,
        target: SendTarget,
        lane: SendLane,
    ) -> Queue[QueueItem]:
        if target is SendTarget.ONEBOT:
            if lane is SendLane.MESSAGE:
                return self.onebot_queue
            if lane is SendLane.EVENT:
                return self.onebot_event_queue
            if lane is SendLane.SYSTEM:
                return self.onebot_system_queue
        if target is SendTarget.TELEGRAM:
            if lane is SendLane.MESSAGE:
                return self.telegram_queue
            if lane is SendLane.EVENT:
                return self.telegram_event_queue
            if lane is SendLane.SYSTEM:
                return self.telegram_system_queue
        raise ValueError(f"未知发送目标或 lane: {target!r}, {lane!r}")


message_bus = MessageBus()
