import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from src.bus import MessageBus
from src.messages import SendTarget, SendTask
from src.notice import enqueue_bridge_notice
from src.qbot import QGateway


class BridgeNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueues_independent_tasks_for_both_sides(self) -> None:
        bus = MessageBus()
        telegram_send = AsyncMock()
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=1))

        enqueue_bridge_notice(
            telegram_send,
            cast(QGateway, gateway),
            q_group_id=123,
            text="状态提示",
            bus=bus,
        )

        telegram_task = await bus.telegram_queue.get()
        onebot_task = await bus.onebot_queue.get()
        try:
            self.assertIsInstance(telegram_task, SendTask)
            self.assertIsInstance(onebot_task, SendTask)
            assert isinstance(telegram_task, SendTask)
            assert isinstance(onebot_task, SendTask)
            self.assertIs(telegram_task.target, SendTarget.TELEGRAM)
            self.assertIs(onebot_task.target, SendTarget.ONEBOT)
            self.assertEqual(telegram_task.max_attempts, 1)
            self.assertEqual(onebot_task.max_attempts, 1)
            await telegram_task.send()
            await onebot_task.send()
        finally:
            bus.telegram_queue.task_done()
            bus.onebot_queue.task_done()

        telegram_send.assert_awaited_once_with()
        gateway.send_group_message.assert_awaited_once_with(
            group_id=123,
            message=[{"type": "text", "data": {"text": "状态提示"}}],
        )

    async def test_one_side_failure_does_not_block_the_other(self) -> None:
        bus = MessageBus()
        telegram_send = AsyncMock(side_effect=RuntimeError("telegram failed"))
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=1))
        enqueue_bridge_notice(
            telegram_send,
            cast(QGateway, gateway),
            q_group_id=123,
            text="状态提示",
            bus=bus,
        )
        with patch("src.bus.baselog.exception"):
            telegram_consumer = asyncio.create_task(bus.consume(SendTarget.TELEGRAM))
            onebot_consumer = asyncio.create_task(bus.consume(SendTarget.ONEBOT))
            try:
                await bus.join(SendTarget.TELEGRAM)
                await bus.join(SendTarget.ONEBOT)
            finally:
                telegram_consumer.cancel()
                onebot_consumer.cancel()
                await asyncio.gather(
                    telegram_consumer,
                    onebot_consumer,
                    return_exceptions=True,
                )

        telegram_send.assert_awaited_once()
        gateway.send_group_message.assert_awaited_once()
        self.assertTrue(bus.telegram_queue.empty())
        self.assertTrue(bus.onebot_queue.empty())

    async def test_full_target_queue_only_drops_that_side(self) -> None:
        bus = MessageBus(maxsize=1)
        await bus.put(SendTask(target=SendTarget.TELEGRAM, send=AsyncMock()))
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=1))

        with patch("src.notice.baselog.error") as error:
            enqueue_bridge_notice(
                AsyncMock(),
                cast(QGateway, gateway),
                q_group_id=123,
                text="状态提示",
                bus=bus,
            )

        self.assertEqual(bus.telegram_queue.qsize(), 1)
        self.assertEqual(bus.onebot_queue.qsize(), 1)
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
