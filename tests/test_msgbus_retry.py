import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.bus import MessageBus
from src.messages import (
    FailureAction,
    SendLane,
    SendTarget,
    SendTargetUnavailableError,
    SendTask,
)


@pytest.mark.asyncio
class TestMessageBusRetry:
    async def _run_until_idle(self, bus: MessageBus, target: SendTarget) -> None:
        dispatcher = asyncio.create_task(bus.dispatch_retries())
        consumer = asyncio.create_task(bus.consume(target))
        try:
            await bus.join(target)
        finally:
            consumer.cancel()
            dispatcher.cancel()
            await asyncio.gather(consumer, dispatcher, return_exceptions=True)

    async def test_target_queues_are_isolated(self) -> None:
        bus = MessageBus()
        onebot_task = SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        telegram_task = SendTask(target=SendTarget.TELEGRAM, send=AsyncMock())

        await bus.put(onebot_task)
        await bus.put(telegram_task)

        assert await bus.onebot_queue.get() is onebot_task
        bus.onebot_queue.task_done()
        assert await bus.telegram_queue.get() is telegram_task
        bus.telegram_queue.task_done()

    async def test_lanes_are_isolated_for_the_same_target(self) -> None:
        bus = MessageBus()
        message = SendTask(target=SendTarget.TELEGRAM, send=AsyncMock())
        event = SendTask(
            target=SendTarget.TELEGRAM,
            lane=SendLane.EVENT,
            send=AsyncMock(),
        )
        system = SendTask(
            target=SendTarget.TELEGRAM,
            lane=SendLane.SYSTEM,
            send=AsyncMock(),
        )

        await bus.put(message)
        await bus.put(event)
        await bus.put(system)

        assert await bus.telegram_queue.get() is message
        bus.telegram_queue.task_done()
        assert await bus.telegram_event_queue.get() is event
        bus.telegram_event_queue.task_done()
        assert await bus.telegram_system_queue.get() is system
        bus.telegram_system_queue.task_done()

    async def test_retry_uses_separate_queue_and_succeeds_on_third_attempt(self) -> None:
        bus = MessageBus()
        send = AsyncMock(side_effect=[RuntimeError(), RuntimeError(), None])
        finalize = AsyncMock()
        exhausted = AsyncMock()
        task = SendTask(
            target=SendTarget.ONEBOT,
            send=send,
            failure_action=lambda error: FailureAction.RETRY,
            max_attempts=3,
            on_failed=exhausted,
            finalize=finalize,
        )
        await bus.put(task)

        await self._run_until_idle(bus, SendTarget.ONEBOT)

        assert send.await_count == 3
        exhausted.assert_not_awaited()
        finalize.assert_awaited_once_with()

    async def test_exhausted_callback_runs_after_three_failures(self) -> None:
        bus = MessageBus()
        error = RuntimeError("failed")
        send = AsyncMock(side_effect=error)
        exhausted = AsyncMock()
        finalize = AsyncMock()
        task = SendTask(
            target=SendTarget.TELEGRAM,
            send=send,
            failure_action=lambda failure: FailureAction.RETRY,
            max_attempts=3,
            on_failed=exhausted,
            finalize=finalize,
        )
        await bus.put(task)

        await self._run_until_idle(bus, SendTarget.TELEGRAM)

        assert send.await_count == 3
        exhausted.assert_awaited_once_with(error)
        finalize.assert_awaited_once_with()

    async def test_retry_moves_to_target_queue_tail(self) -> None:
        bus = MessageBus()
        order: list[str] = []
        attempts = 0

        async def send_first() -> None:
            nonlocal attempts
            attempts += 1
            order.append(f"A{attempts}")
            if attempts == 1:
                raise RuntimeError

        async def send_second() -> None:
            order.append("B")

        await bus.put(
            SendTask(
                target=SendTarget.ONEBOT,
                send=send_first,
                failure_action=lambda error: FailureAction.RETRY,
                max_attempts=2,
            )
        )
        await bus.put(SendTask(target=SendTarget.ONEBOT, send=send_second))

        await self._run_until_idle(bus, SendTarget.ONEBOT)

        assert order == ["A1", "B", "A2"]

    async def test_defer_does_not_count_failure_or_finalize(self) -> None:
        bus = MessageBus()
        finalize = AsyncMock()
        task = SendTask(
            target=SendTarget.ONEBOT,
            send=AsyncMock(side_effect=RuntimeError()),
            failure_action=Mock(return_value=FailureAction.DEFER),
            max_attempts=3,
            finalize=finalize,
        )
        await bus.put(task)

        await bus.consume(SendTarget.ONEBOT)

        assert task.failures == 0
        assert await bus.onebot_retry_queue.get() is task
        bus.onebot_retry_queue.task_done()
        finalize.assert_not_awaited()

    async def test_blocked_onebot_retry_does_not_block_telegram_join(self) -> None:
        bus = MessageBus(maxsize=1)
        await bus.onebot_queue.put(
            SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        )
        await bus.onebot_retry_queue.put(
            SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        )

        dispatcher = asyncio.create_task(bus.dispatch_retries())
        try:
            await asyncio.wait_for(bus.join(SendTarget.TELEGRAM), timeout=0.1)
        finally:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)

    async def test_pausing_target_unblocks_waiting_producer(self) -> None:
        bus = MessageBus(maxsize=1)
        first = SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        second = SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        await bus.put(first)
        producer = asyncio.create_task(bus.put(second))
        await asyncio.sleep(0)
        assert not producer.done()

        bus.pause_target(SendTarget.ONEBOT)
        with pytest.raises(
            SendTargetUnavailableError,
            match="发送目标在等待队列空间时停止",
        ):
            await producer

        assert await bus.onebot_queue.get() is first
        bus.onebot_queue.task_done()

    async def test_cancelled_consumer_finishes_finalizer_and_task_done(self) -> None:
        bus = MessageBus()
        finalize_started = asyncio.Event()
        release_finalize = asyncio.Event()

        async def finalize() -> None:
            finalize_started.set()
            await release_finalize.wait()

        await bus.put(
            SendTask(
                target=SendTarget.ONEBOT,
                send=AsyncMock(),
                finalize=finalize,
            )
        )
        consumer = asyncio.create_task(bus.consume(SendTarget.ONEBOT))
        await finalize_started.wait()
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done()

        release_finalize.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.wait_for(bus.join(SendTarget.ONEBOT), timeout=0.1)

    async def test_pausing_target_cancels_active_producer_operation(self) -> None:
        bus = MessageBus()
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def operation() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        producer = asyncio.create_task(
            bus.run_while_target_available(SendTarget.ONEBOT, operation())
        )
        await started.wait()
        bus.pause_target(SendTarget.ONEBOT)

        with pytest.raises(SendTargetUnavailableError):
            await producer
        assert cleaned.is_set()

    async def test_pause_after_queue_commit_preserves_transferred_task(self) -> None:
        bus = MessageBus(maxsize=1)
        first = SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        second = SendTask(target=SendTarget.ONEBOT, send=AsyncMock())
        await bus.put(first)
        committed = asyncio.Event()
        original_put = bus.onebot_queue.put

        async def observed_put(item) -> None:
            await original_put(item)
            committed.set()

        async def operation() -> None:
            await bus.put(second)

        with patch.object(
            bus.onebot_queue,
            "put",
            side_effect=observed_put,
        ) as put:
            producer = asyncio.create_task(
                bus.run_while_target_available(SendTarget.ONEBOT, operation())
            )
            while not put.await_count:
                await asyncio.sleep(0)
            assert await bus.onebot_queue.get() is first
            bus.onebot_queue.task_done()
            await committed.wait()
            bus.pause_target(SendTarget.ONEBOT)
            await producer

        assert await bus.onebot_queue.get() is second
        bus.onebot_queue.task_done()

    async def test_drop_failure_runs_final_failure_callback(self) -> None:
        bus = MessageBus()
        error = RuntimeError("not retryable")
        failed = AsyncMock()
        task = SendTask(
            target=SendTarget.ONEBOT,
            send=AsyncMock(side_effect=error),
            on_failed=failed,
        )
        await bus.put(task)

        await self._run_until_idle(bus, SendTarget.ONEBOT)

        failed.assert_awaited_once_with(error)
