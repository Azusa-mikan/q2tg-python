from __future__ import annotations

"""按目标平台隔离的发送队列和独立重试调度队列。"""

import asyncio
from asyncio import Queue, QueueFull
from collections.abc import Awaitable
from dataclasses import dataclass

from src.lifecycle import await_completion_on_cancel
from src.log import baselog
from src.messages import (
    FailureAction,
    SendLane,
    SendTarget,
    SendTargetUnavailableError,
    SendTask,
)
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
        # 每个目标 lane 使用独立的无界重试队列。某个断线目标的发送队列已满时，
        # 其 dispatcher 可以继续等待，但不会阻塞另一目标的重试和关停排空。
        self.onebot_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self.telegram_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self.onebot_event_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self.telegram_event_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self.onebot_system_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self.telegram_system_retry_queue: Queue[SendTask | Shutdown] = Queue()
        self._target_stopped = {target: asyncio.Event() for target in SendTarget}
        self._target_available = {target: asyncio.Event() for target in SendTarget}
        for available in self._target_available.values():
            available.set()

    async def put(self, task: SendTask) -> None:
        """按任务目标路由，并对异步生产者施加背压。"""
        queue = self._target_queue(task.target, task.lane)
        stopped = self._target_stopped[task.target]
        if stopped.is_set():
            raise SendTargetUnavailableError(f"发送目标已停止: {task.target.name}")
        try:
            queue.put_nowait(task)
        except QueueFull:
            putter = asyncio.create_task(queue.put(task))
            stop_waiter = asyncio.create_task(stopped.wait())
            try:
                try:
                    done, _ = await asyncio.wait(
                        (putter, stop_waiter),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    # queue.put 已完成即表示队列接管任务；此后不能让调用方按未入队清理。
                    if putter.done() and not putter.cancelled():
                        await putter
                        emit_runtime_event("send.enqueued", task.label)
                        return
                    raise
                if putter not in done:
                    putter.cancel()
                    await asyncio.gather(putter, return_exceptions=True)
                    raise SendTargetUnavailableError(
                        f"发送目标在等待队列空间时停止: {task.target.name}"
                    )
                await putter
            finally:
                if not putter.done():
                    putter.cancel()
                await asyncio.gather(putter, return_exceptions=True)
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
        emit_runtime_event("send.enqueued", task.label)

    async def run_while_target_available[T](
        self,
        target: SendTarget,
        operation: Awaitable[T],
    ) -> T:
        """目标暂停时取消仍在执行的 producer 操作并报告不可用。"""
        task = asyncio.ensure_future(operation)
        stopped = self._target_stopped[target]
        if stopped.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise SendTargetUnavailableError(f"发送目标已停止: {target.name}")
        stop_waiter = asyncio.create_task(stopped.wait())
        try:
            done, _ = await asyncio.wait(
                (task, stop_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return await task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not task.cancelled() and task.exception() is None:
                return task.result()
            raise SendTargetUnavailableError(
                f"发送目标在 producer 执行期间停止: {target.name}"
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    def put_nowait(self, task: SendTask) -> bool:
        """按任务目标立即入队；满队列时返回 False。"""
        if self._target_stopped[task.target].is_set():
            return False
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
        """在应用关闭时停止全部重试调度器。"""
        for target in SendTarget:
            for lane in SendLane:
                await self._retry_queue(target, lane).put(SHUTDOWN)

    async def join(
        self,
        target: SendTarget,
        lane: SendLane = SendLane.MESSAGE,
    ) -> None:
        """等待指定目标和 lane 及其可能产生的重试任务稳定排空。"""
        queue = self._target_queue(target, lane)
        retry_queue = self._retry_queue(target, lane)
        while True:
            await queue.join()
            await retry_queue.join()
            await queue.join()
            # task_done 与下一次 retry put 之间不存在 await，但调度器把 retry
            # 重新投递目标队列后仍需给消费者一个运行机会再判断稳定状态。
            await asyncio.sleep(0)
            if queue.empty() and retry_queue.empty():
                return

    async def dispatch_retries(self) -> None:
        """按目标 lane 独立地把失败任务重新投递到发送队列尾部。"""
        async with asyncio.TaskGroup() as dispatchers:
            for target in SendTarget:
                for lane in SendLane:
                    dispatchers.create_task(
                        self._dispatch_retries(target, lane),
                        name=f"{target.name.lower()}-{lane.name.lower()}-retry-dispatcher",
                    )

    async def _dispatch_retries(
        self,
        target: SendTarget,
        lane: SendLane,
    ) -> None:
        retry_queue = self._retry_queue(target, lane)
        while True:
            item = await retry_queue.get()
            try:
                if isinstance(item, Shutdown):
                    return
                try:
                    await self.put(item)
                except SendTargetUnavailableError:
                    retry_queue.put_nowait(item)
                    await self._target_available[target].wait()
            finally:
                retry_queue.task_done()

    def pause_target(self, target: SendTarget) -> None:
        """停止目标的新生产者，并唤醒正在等待队列空间的 put。"""
        self._target_available[target].clear()
        self._target_stopped[target].set()

    def resume_target(self, target: SendTarget) -> None:
        """恢复目标生产者和重试分发。"""
        self._target_stopped[target].clear()
        self._target_available[target].set()

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
                        self._retry_queue(item.target, item.lane).put_nowait(item)
                        return
                    if action is FailureAction.RETRY:
                        item.failures += 1
                        if item.failures < item.max_attempts:
                            requeued = True
                            self._retry_queue(item.target, item.lane).put_nowait(item)
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
                    self._retry_queue(item.target, item.lane).put_nowait(item)
                    emit_runtime_event("send.cancelled", item.label)
                    raise
            finally:
                try:
                    if isinstance(item, SendTask) and not requeued:
                        finalized = await await_completion_on_cancel(
                            self._finalize(item)
                        )
                        if sent and finalized:
                            emit_runtime_event("send.succeeded", item.label)
                finally:
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

    def _retry_queue(
        self,
        target: SendTarget,
        lane: SendLane,
    ) -> Queue[SendTask | Shutdown]:
        if target is SendTarget.ONEBOT:
            if lane is SendLane.MESSAGE:
                return self.onebot_retry_queue
            if lane is SendLane.EVENT:
                return self.onebot_event_retry_queue
            if lane is SendLane.SYSTEM:
                return self.onebot_system_retry_queue
        if target is SendTarget.TELEGRAM:
            if lane is SendLane.MESSAGE:
                return self.telegram_retry_queue
            if lane is SendLane.EVENT:
                return self.telegram_event_retry_queue
            if lane is SendLane.SYSTEM:
                return self.telegram_system_retry_queue
        raise ValueError(f"未知重试目标或 lane: {target!r}, {lane!r}")

    def retry_queue_size(self) -> int:
        """返回全部目标 lane 的待重试任务数。"""
        return sum(
            self._retry_queue(target, lane).qsize()
            for target in SendTarget
            for lane in SendLane
        )


message_bus = MessageBus()
