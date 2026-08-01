import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.forwarding import (
    finalize_telegram_message,
    onebot_forward_task,
    telegram_forward_task,
)
from src.media import MediaFile, media_cache
from src.messages import (
    FailureAction,
    MediaTooLargeError,
    OneBotMessage,
    OneBotSendError,
    SendTarget,
    SendTask,
    TelegramMessage,
)
from src.qbot import QGateway


class ForwardTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        media_cache.close()

    async def test_finalize_releases_media_cache_pin_idempotently(self) -> None:
        media = await MediaFile.create(filename="voice.ogg", media_type="audio/ogg")
        media.write(b"voice")
        media_id = media_cache.set_media_batch((media,), pinned=True)[0]
        message = TelegramMessage(
            message_ids=(1,),
            group_id=-456,
            user_id=2,
            sender_name="TG User",
            text=None,
            media_ids=(media_id,),
            media_cache_pinned=True,
        )

        await finalize_telegram_message(message)
        await finalize_telegram_message(message)

        self.assertFalse(message.media_cache_pinned)
        self.assertEqual(media_cache._media[media_id].pins, 0)

    async def test_telegram_target_exhaustion_only_notifies_onebot(self) -> None:
        bus = MessageBus()
        bot = cast(ExtBot[None], SimpleNamespace())
        client = cast(httpx.AsyncClient, SimpleNamespace())
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=99))
        message = OneBotMessage(
            message_id=1,
            group_id=123,
            user_id=2,
            sender_name="Onebot User",
            message=[{"type": "text", "data": {"text": "message"}}],
        )
        task = onebot_forward_task(message, bot, client, cast(QGateway, gateway))
        task.send = AsyncMock(side_effect=RuntimeError("telegram failed"))
        await bus.put(task)

        with patch("src.notice.message_bus", bus):
            dispatcher = asyncio.create_task(bus.dispatch_retries())
            consumer = asyncio.create_task(bus.consume(SendTarget.TELEGRAM))
            try:
                await bus.join(SendTarget.TELEGRAM)
            finally:
                consumer.cancel()
                dispatcher.cancel()
                await asyncio.gather(
                    consumer,
                    dispatcher,
                    return_exceptions=True,
                )

        self.assertEqual(task.send.await_count, 3)
        onebot_notice = await bus.onebot_queue.get()
        try:
            self.assertIsInstance(onebot_notice, SendTask)
            assert isinstance(onebot_notice, SendTask)
            await onebot_notice.send()
        finally:
            bus.onebot_queue.task_done()
        gateway.send_group_message.assert_awaited_once_with(
            group_id=123,
            message=[
                {
                    "type": "text",
                    "data": {
                        "text": "消息转发到 Telegram 连续失败 3 次，请稍后重试。",
                    },
                }
            ],
        )

    async def test_onebot_target_exhaustion_disables_forwarding(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        gateway = cast(QGateway, SimpleNamespace())
        message = TelegramMessage(
            message_ids=(1,),
            group_id=-456,
            user_id=2,
            sender_name="TG User",
            text="message",
        )
        task = telegram_forward_task(message, gateway, cast(ExtBot[None], bot))
        sql = SimpleNamespace(
            set_tg_forward_enabled=AsyncMock(),
            get_q_group=AsyncMock(return_value=123),
        )

        with (
            patch("src.forwarding.sql", sql),
            patch("src.forwarding.enqueue_bridge_notice") as notice,
        ):
            assert task.on_failed is not None
            await task.on_failed(OneBotSendError())

        sql.set_tg_forward_enabled.assert_awaited_once_with(-456, False)
        notice.assert_called_once()

    async def test_non_retryable_onebot_failure_notifies_source_telegram_group(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        message = TelegramMessage(
            message_ids=(1,),
            group_id=-456,
            user_id=2,
            sender_name="TG User",
            text="message",
        )
        task = telegram_forward_task(
            message,
            cast(QGateway, SimpleNamespace()),
            cast(ExtBot[None], bot),
        )

        with patch("src.forwarding.enqueue_telegram_notice") as notice:
            assert task.on_failed is not None
            await task.on_failed(RuntimeError("local failure"))

        notice.assert_called_once()
        send = notice.call_args.args[0]
        await send()
        bot.send_message.assert_awaited_once_with(
            chat_id=-456,
            text="消息发送到 Onebot 失败，请稍后重试。",
        )

    async def test_oversized_onebot_media_drops_and_reports_exact_reason(self) -> None:
        gateway = cast(
            QGateway,
            SimpleNamespace(send_group_message=AsyncMock(return_value=99)),
        )
        message = OneBotMessage(
            message_id=1,
            group_id=123,
            user_id=2,
            sender_name="Onebot User",
            message=[{"type": "image", "data": {"url": "https://example.test/image"}}],
        )
        task = onebot_forward_task(
            message,
            cast(ExtBot[None], SimpleNamespace()),
            cast(httpx.AsyncClient, SimpleNamespace()),
            gateway,
        )
        error = MediaTooLargeError("Onebot 媒体超过 20 MB，无法转发")

        self.assertIs(task.failure_action(error), FailureAction.DROP)
        with patch("src.forwarding.enqueue_onebot_notice") as notice:
            assert task.on_failed is not None
            await task.on_failed(error)

        notice.assert_called_once_with(
            gateway,
            q_group_id=123,
            text="Onebot 媒体超过 20 MB，无法转发",
        )


if __name__ == "__main__":
    unittest.main()
