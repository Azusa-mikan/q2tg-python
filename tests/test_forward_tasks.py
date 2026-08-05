import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telegram.error import NetworkError, TimedOut
from telegram.ext import ExtBot

from src.bus import MessageBus
from src.forwarding import (
    finalize_telegram_message,
    forward_onebot_to_telegram,
    forward_telegram_to_onebot,
    onebot_forward_task,
    telegram_forward_task,
)
from src.mapping_outbox import PendingMessageMapping
from src.media import MediaFile, media_cache
from src.messages import (
    FailureAction,
    MediaTooLargeError,
    MessageMappingError,
    OneBotMessage,
    OneBotResultUnknownError,
    OneBotSendError,
    SendTarget,
    SendTask,
    TelegramMessage,
)
from src.qbot import QGateway
from src.sql import MESSAGE_RETENTION, MessageMapping


@pytest.mark.asyncio
class TestForwardTasks:
    @pytest.fixture(autouse=True)
    def close_media_cache(self):
        yield
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

        assert not message.media_cache_pinned
        assert media_cache._media[media_id].pins == 0

    async def test_onebot_mapping_retry_does_not_resend_telegram_message(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=201))
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-456),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=456,
            sender_name="Example User",
            message=[{"type": "text", "data": {"text": "message"}}],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch("src.forwarding.sql", database),
                patch(
                    "src.forwarding.mapping_outbox.enqueue",
                    new_callable=AsyncMock,
                ) as enqueue,
                patch("src.forwarding.baselog.exception"),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_awaited_once()
        database.set_message_mapping.assert_awaited_once()
        enqueue.assert_awaited_once()
        assert message.tg_forward_complete

    async def test_pending_mapping_prevents_duplicate_when_database_is_down(self) -> None:
        pending = PendingMessageMapping(
            q_group_id=123,
            q_message_ids=(101,),
            tg_chat_id=-456,
            tg_message_ids=(201,),
            q_user_id=789,
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(side_effect=RuntimeError("database unavailable"))
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=789,
            sender_name="Example User",
            message=[{"type": "text", "data": {"text": "message"}}],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch("src.forwarding.sql", database),
                patch(
                    "src.forwarding.mapping_outbox.get_tg_message",
                    side_effect=[None, pending],
                ),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_not_awaited()
        assert message.tg_chat_id == pending.tg_chat_id
        assert message.tg_message_ids == [201]

    async def test_newer_pending_mapping_wins_over_readable_old_database_mapping(
        self,
    ) -> None:
        pending = PendingMessageMapping(
            q_group_id=123,
            q_message_ids=(101,),
            tg_chat_id=-456,
            tg_message_ids=(202,),
            q_user_id=789,
        )
        old = MessageMapping(
            q_group_id=123,
            q_message_ids=(101,),
            tg_chat_id=-456,
            tg_message_ids=(201,),
            q_user_id=789,
            tg_user_id=None,
            expires_at=pending.created_at + MESSAGE_RETENTION - 1,
        )
        database = SimpleNamespace(get_tg_message=AsyncMock(return_value=old))
        bot = SimpleNamespace(send_message=AsyncMock())
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=789,
            sender_name="Example User",
            message=[{"type": "text", "data": {"text": "message"}}],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch("src.forwarding.sql", database),
                patch(
                    "src.forwarding.mapping_outbox.get_tg_message",
                    side_effect=[None, pending],
                ),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_not_awaited()
        assert message.tg_message_ids == [202]

    async def test_telegram_mapping_retry_does_not_resend_onebot_message(self) -> None:
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=301))
        database = SimpleNamespace(
            get_q_group=AsyncMock(return_value=123),
            get_tg_forward_enabled=AsyncMock(return_value=True),
            set_message_mapping=AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        message = TelegramMessage(
            message_ids=(201,),
            group_id=-456,
            user_id=789,
            sender_name="Example User",
            text="message",
        )

        with (
            patch("src.forwarding.sql", database),
            patch(
                "src.forwarding.mapping_outbox.enqueue",
                new_callable=AsyncMock,
            ) as enqueue,
            patch("src.forwarding.baselog.exception"),
        ):
            await forward_telegram_to_onebot(message, cast(QGateway, gateway))

        gateway.send_group_message.assert_awaited_once()
        database.set_message_mapping.assert_awaited_once()
        enqueue.assert_awaited_once()
        assert message.q_forward_complete

    async def test_mapping_failure_is_reported_when_outbox_also_fails(self) -> None:
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=201))
        )
        database = SimpleNamespace(
            get_tg_message=AsyncMock(return_value=None),
            get_tg_group=AsyncMock(return_value=-456),
            get_id_show_enabled=AsyncMock(return_value=False),
            set_message_mapping=AsyncMock(side_effect=RuntimeError("database unavailable")),
        )
        message = OneBotMessage(
            message_id=101,
            group_id=123,
            user_id=456,
            sender_name="Example User",
            message=[{"type": "text", "data": {"text": "message"}}],
        )

        async with httpx.AsyncClient() as client:
            with (
                patch("src.forwarding.sql", database),
                patch(
                    "src.forwarding.mapping_outbox.enqueue",
                    new_callable=AsyncMock,
                    side_effect=OSError("disk full"),
                ),
                pytest.raises(MessageMappingError),
            ):
                await forward_onebot_to_telegram(
                    message,
                    cast(ExtBot[None], bot),
                    client,
                )

        bot.send_message.assert_awaited_once()

    async def test_telegram_target_exhaustion_only_notifies_onebot(self) -> None:
        bus = MessageBus()
        bot = cast(ExtBot[None], SimpleNamespace())
        client = cast(httpx.AsyncClient, SimpleNamespace())
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=99))
        message = OneBotMessage(
            message_id=1,
            group_id=123,
            user_id=2,
            sender_name="OneBot User",
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

        assert task.send.await_count == 3
        onebot_notice = await bus.onebot_system_queue.get()
        try:
            assert isinstance(onebot_notice, SendTask)
            await onebot_notice.send()
        finally:
            bus.onebot_system_queue.task_done()
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

    async def test_unknown_onebot_result_is_not_retried_or_disable_forwarding(self) -> None:
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
        error = OneBotResultUnknownError("result unknown")

        assert task.failure_action(error) is FailureAction.DROP
        with patch("src.forwarding.enqueue_telegram_notice") as notice:
            assert task.on_failed is not None
            await task.on_failed(error)

        send = notice.call_args.args[0]
        await send()
        assert "结果未知" in bot.send_message.await_args.kwargs["text"]

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
            text="消息发送到 OneBot 失败，请稍后重试。",
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
            sender_name="OneBot User",
            message=[{"type": "image", "data": {"url": "https://example.test/image"}}],
        )
        task = onebot_forward_task(
            message,
            cast(ExtBot[None], SimpleNamespace()),
            cast(httpx.AsyncClient, SimpleNamespace()),
            gateway,
        )
        error = MediaTooLargeError("OneBot 媒体超过 50 MB，无法转发")

        assert task.failure_action(error) is FailureAction.DROP
        with patch("src.forwarding.enqueue_onebot_notice") as notice:
            assert task.on_failed is not None
            await task.on_failed(error)

        notice.assert_called_once_with(
            gateway,
            q_group_id=123,
            text="OneBot 媒体超过 50 MB，无法转发",
            label="onebot-media-rejected:123:1",
        )

    async def test_telegram_timeout_is_not_retried_and_reports_unknown_result(self) -> None:
        bus = MessageBus()
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=99))
        message = OneBotMessage(
            message_id=2,
            group_id=123,
            user_id=2,
            sender_name="OneBot User",
            message=[{"type": "video", "data": {"url": "https://example.test/video"}}],
        )
        task = onebot_forward_task(
            message,
            cast(ExtBot[None], SimpleNamespace()),
            cast(httpx.AsyncClient, SimpleNamespace()),
            cast(QGateway, gateway),
        )
        task.send = AsyncMock(side_effect=TimedOut("upload timed out"))
        await bus.put(task)

        with patch("src.notice.message_bus", bus):
            consumer = asyncio.create_task(bus.consume(SendTarget.TELEGRAM))
            try:
                await bus.join(SendTarget.TELEGRAM)
            finally:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

        task.send.assert_awaited_once()
        onebot_notice = await bus.onebot_system_queue.get()
        try:
            assert isinstance(onebot_notice, SendTask)
            await onebot_notice.send()
        finally:
            bus.onebot_system_queue.task_done()
        text = gateway.send_group_message.await_args.kwargs["message"][0]["data"]["text"]
        assert "发送结果未知" in text
        assert "未自动重试" in text

    async def test_other_telegram_network_error_is_not_retried(self) -> None:
        task = onebot_forward_task(
            OneBotMessage(
                message_id=3,
                group_id=123,
                user_id=2,
                sender_name="OneBot User",
                message=[{"type": "text", "data": {"text": "message"}}],
            ),
            cast(ExtBot[None], SimpleNamespace()),
            cast(httpx.AsyncClient, SimpleNamespace()),
            cast(QGateway, SimpleNamespace()),
        )

        assert task.failure_action(NetworkError("connection reset")) is FailureAction.DROP
