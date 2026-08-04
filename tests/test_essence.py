import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import WebSocket
from telegram import Update
from telegram.ext import ContextTypes, ExtBot

from src.bus import MessageBus
from src.messages import SendLane, SendTarget, SendTask
from src.qbot import QGateway, receive_onebot_event
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


@pytest.mark.asyncio
class TestEssenceGateway:
    async def test_essence_methods_use_expected_actions(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        for method, action in (
            (gateway.set_essence_message, "set_essence_msg"),
            (gateway.delete_essence_message, "delete_essence_msg"),
        ):
            task = asyncio.create_task(method(-1_001))
            while websocket.send_json.await_count < 1:
                await asyncio.sleep(0)
            request = websocket.send_json.await_args.args[0]
            gateway.resolve_response(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": None,
                    "echo": request["echo"],
                }
            )
            await task
            assert request["action"] == action
            assert request["params"] == {"message_id": -1_001}
            websocket.send_json.reset_mock()


@pytest.mark.asyncio
class TestEssenceBridge:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_bridge(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "essence.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -100_123)
        await self.sql.set_message_mapping(
            q_group_id=123,
            q_message_ids=(-1_001, -1_002),
            tg_chat_id=-100_123,
            tg_message_ids=(201, 202),
        )
        self.bus = MessageBus()
        self.pin_chat_message = AsyncMock()
        self.unpin_chat_message = AsyncMock()
        self.bot = cast(
            ExtBot[None],
            SimpleNamespace(
                pin_chat_message=self.pin_chat_message,
                unpin_chat_message=self.unpin_chat_message,
            ),
        )
        self.client = cast(httpx.AsyncClient, SimpleNamespace())
        try:
            yield
        finally:
            await self.sql.close()
            self.directory.cleanup()

    async def test_onebot_add_and_delete_essence_update_all_telegram_messages(
        self,
    ) -> None:
        for sub_type in ("add", "delete"):
            with patch("src.qbot.message_bus", self.bus):
                await receive_onebot_event(
                    {
                        "post_type": "notice",
                        "notice_type": "essence",
                        "sub_type": sub_type,
                        "group_id": 123,
                        "message_id": -1_001,
                    },
                    self.bot,
                    self.client,
                )
            task = await self.bus.telegram_event_queue.get()
            try:
                assert isinstance(task, SendTask)
                assert task.target is SendTarget.TELEGRAM
                assert task.lane is SendLane.EVENT
                with patch("src.forwarding.sql", self.sql):
                    await task.send()
            finally:
                self.bus.telegram_event_queue.task_done()

        assert self.pin_chat_message.await_count == 2
        self.pin_chat_message.assert_any_await(
            chat_id=-100_123,
            message_id=201,
            disable_notification=True,
        )
        self.pin_chat_message.assert_any_await(
            chat_id=-100_123,
            message_id=202,
            disable_notification=True,
        )
        assert self.unpin_chat_message.await_count == 2
        self.unpin_chat_message.assert_any_await(
            chat_id=-100_123,
            message_id=201,
        )
        self.unpin_chat_message.assert_any_await(
            chat_id=-100_123,
            message_id=202,
        )

    async def test_malformed_essence_event_is_not_queued(self) -> None:
        with (
            patch("src.qbot.message_bus", self.bus),
            patch("src.qbot.qlog.warning") as warning,
        ):
            await receive_onebot_event(
                {
                    "post_type": "notice",
                    "notice_type": "essence",
                    "sub_type": "replace",
                    "group_id": 123,
                    "message_id": -1_001,
                },
                self.bot,
                self.client,
            )

        assert self.bus.telegram_event_queue.empty()
        warning.assert_called_once()

    async def test_telegram_pin_sets_all_mapped_onebot_messages(self) -> None:
        gateway = SimpleNamespace(set_essence_message=AsyncMock())
        service_message = SimpleNamespace(
            chat_id=-100_123,
            from_user=SimpleNamespace(id=801),
            pinned_message=SimpleNamespace(message_id=201),
        )
        update = cast(Update, SimpleNamespace(effective_message=service_message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=900)),
        )
        handler = TGhandlers()

        with (
            patch("src.tgbot.handlers.message_bus", self.bus),
            patch("src.tgbot.handlers.q_gateway", gateway),
        ):
            await handler.receive_pinned_message(update, context)
        task = await self.bus.onebot_event_queue.get()
        try:
            assert isinstance(task, SendTask)
            assert task.target is SendTarget.ONEBOT
            assert task.lane is SendLane.EVENT
            with patch("src.forwarding.sql", self.sql):
                await task.send()
        finally:
            self.bus.onebot_event_queue.task_done()

        assert gateway.set_essence_message.await_args_list == [call(-1_001), call(-1_002)]

    async def test_bot_generated_pin_service_message_is_ignored(self) -> None:
        handler = TGhandlers()
        update = cast(
            Update,
            SimpleNamespace(
                effective_message=SimpleNamespace(
                    chat_id=-100_123,
                    from_user=SimpleNamespace(id=900),
                    pinned_message=SimpleNamespace(message_id=201),
                )
            ),
        )
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=900)),
        )

        with patch("src.tgbot.handlers.message_bus", self.bus):
            await handler.receive_pinned_message(update, context)

        assert self.bus.onebot_event_queue.empty()
