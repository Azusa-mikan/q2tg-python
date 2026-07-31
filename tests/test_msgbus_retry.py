import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from src.bus import MessageBus
from src.messages import FailureAction, SendTarget, SendTask


class MessageBusRetryTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertIs(await bus.onebot_queue.get(), onebot_task)
        bus.onebot_queue.task_done()
        self.assertIs(await bus.telegram_queue.get(), telegram_task)
        bus.telegram_queue.task_done()

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

        self.assertEqual(send.await_count, 3)
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

        self.assertEqual(send.await_count, 3)
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

        self.assertEqual(order, ["A1", "B", "A2"])

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

        self.assertEqual(task.failures, 0)
        self.assertIs(await bus.retry_queue.get(), task)
        bus.retry_queue.task_done()
        finalize.assert_not_awaited()

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


if __name__ == "__main__":
    unittest.main()
