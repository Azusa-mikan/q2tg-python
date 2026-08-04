from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from telegram import Chat, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.bus import MessageBus
from src.config import config
from src.messages import SendTask
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


@pytest.mark.asyncio
class TestBindingNotices:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_database(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "q2tg.db")
        await self.sql.load()
        self.handler = TGhandlers()
        try:
            yield
        finally:
            await self.sql.close()
            self.directory.cleanup()

    async def test_bind_and_unbind_send_each_side_the_other_group_name(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(
                id=-456,
                type="supergroup",
                title="Example Telegram Group",
            ),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(args=["123456789"])
        group = {"group_id": 123_456_789, "group_name": "Example OneBot Group"}
        gateway = SimpleNamespace(
            get_group_list=AsyncMock(return_value=[group]),
            get_group_info=AsyncMock(return_value=group),
            send_group_message=AsyncMock(return_value=1),
        )
        bus = MessageBus()

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.notice.message_bus", bus),
        ):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )
            context.args = []
            await self.handler.unbind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        telegram_tasks = []
        onebot_tasks = []
        while not bus.telegram_system_queue.empty():
            task = await bus.telegram_system_queue.get()
            assert isinstance(task, SendTask)
            telegram_tasks.append(task)
            bus.telegram_system_queue.task_done()
        while not bus.onebot_system_queue.empty():
            task = await bus.onebot_system_queue.get()
            assert isinstance(task, SendTask)
            onebot_tasks.append(task)
            bus.onebot_system_queue.task_done()
        for task in telegram_tasks + onebot_tasks:
            await task.send()

        assert [call.args[0] for call in message.reply_text.await_args_list] == [
                "已绑定群 Example OneBot Group",
                "已解绑群 Example OneBot Group",
            ]
        assert [
                call.kwargs["message"][0]["data"]["text"]
                for call in gateway.send_group_message.await_args_list
            ] == [
                "已绑定群 Example Telegram Group",
                "已解绑群 Example Telegram Group",
            ]
        gateway.get_group_list.assert_awaited_once_with()
        assert gateway.get_group_info.await_count == 2
        gateway.get_group_info.assert_any_await(123_456_789)

    async def test_bind_rejects_group_missing_from_onebot_group_list(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(
                id=-456,
                type="supergroup",
                title="Example Telegram Group",
            ),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(args=["123456789"])
        gateway = SimpleNamespace(
            get_group_list=AsyncMock(return_value=[]),
            get_group_info=AsyncMock(),
        )

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
        ):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("OneBot端未找到该群聊")
        gateway.get_group_info.assert_not_awaited()
        assert await self.sql.get_q_group(-456) is None

    async def test_bind_from_private_chat_uses_explicit_telegram_chat_id(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=900_001, type="private"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        target_chat = Chat(
            id=-1_000_000_000_456,
            type="supergroup",
            title="Example Telegram Group",
        )
        context = SimpleNamespace(
            args=["-1000000000456", "123456789"],
            bot=SimpleNamespace(
                get_chat=AsyncMock(return_value=target_chat),
                send_message=AsyncMock(),
            ),
        )
        group = {"group_id": 123_456_789, "group_name": "Example OneBot Group"}
        gateway = SimpleNamespace(
            get_group_list=AsyncMock(return_value=[group]),
            get_group_info=AsyncMock(return_value=group),
            send_group_message=AsyncMock(return_value=1),
        )
        bus = MessageBus()

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.notice.message_bus", bus),
        ):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        assert await self.sql.get_q_group(-1_000_000_000_456) == 123_456_789
        context.bot.get_chat.assert_awaited_once_with(-1_000_000_000_456)
        telegram_task = await bus.telegram_system_queue.get()
        target_group_task = await bus.telegram_system_queue.get()
        onebot_task = await bus.onebot_system_queue.get()
        assert isinstance(telegram_task, SendTask)
        assert isinstance(target_group_task, SendTask)
        assert isinstance(onebot_task, SendTask)
        await telegram_task.send()
        await target_group_task.send()
        await onebot_task.send()
        message.reply_text.assert_awaited_once_with("已绑定群 Example OneBot Group")
        context.bot.send_message.assert_awaited_once_with(
            chat_id=-1_000_000_000_456,
            text="已绑定群 Example OneBot Group",
        )
        assert gateway.send_group_message.await_args.kwargs["message"][0]["data"]["text"] == (
            "已绑定群 Example Telegram Group"
        )

    async def test_bind_from_private_chat_rejects_inaccessible_chat(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=900_001, type="private"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(
            args=["-1000000000456", "123456789"],
            bot=SimpleNamespace(
                get_chat=AsyncMock(side_effect=BadRequest("Chat not found"))
            ),
        )

        with patch("src.tgbot.handlers.sql", self.sql):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("无法访问指定的 Telegram 聊天")
        assert await self.sql.get_q_group(-1_000_000_000_456) is None

    async def test_bind_from_private_chat_rejects_non_group_chat(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=900_001, type="private"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(
            args=["900002", "123456789"],
            bot=SimpleNamespace(
                get_chat=AsyncMock(
                    return_value=Chat(id=900_002, type="private", first_name="Example")
                )
            ),
        )

        with patch("src.tgbot.handlers.sql", self.sql):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("指定的 Telegram 聊天不是群聊")
        assert await self.sql.get_q_group(900_002) is None

    async def test_bind_reports_and_logs_onebot_error(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(
                id=-456,
                type="supergroup",
                title="Example Telegram Group",
            ),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(args=["123456789"])
        gateway = SimpleNamespace(
            get_group_list=AsyncMock(side_effect=RuntimeError("OneBot failed")),
        )

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.tgbot.handlers.baselog.exception") as log_exception,
        ):
            await self.handler.bind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("OneBot 错误，请检查日志")
        log_exception.assert_called_once()
        assert await self.sql.get_q_group(-456) is None

    async def test_unbind_keeps_mapping_when_group_info_request_fails(self) -> None:
        await self.sql.bind_group(123_456_789, -456)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(
                id=-456,
                type="supergroup",
                title="Example Telegram Group",
            ),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(args=[])
        gateway = SimpleNamespace(
            get_group_info=AsyncMock(side_effect=RuntimeError("OneBot failed")),
        )

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.tgbot.handlers.baselog.exception") as log_exception,
        ):
            await self.handler.unbind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("OneBot 错误，请检查日志")
        log_exception.assert_called_once()
        assert await self.sql.get_q_group(-456) == 123_456_789

    @pytest.mark.parametrize("group_id", ["-1000000000456", "123456789"])
    async def test_unbind_from_private_chat_accepts_either_group_id(
        self,
        group_id: str,
    ) -> None:
        tg_chat_id = -1_000_000_000_456
        q_group_id = 123_456_789
        await self.sql.bind_group(q_group_id, tg_chat_id)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=900_001, type="private"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(
            args=[group_id],
            bot=SimpleNamespace(
                get_chat=AsyncMock(
                    return_value=Chat(
                        id=tg_chat_id,
                        type="supergroup",
                        title="Example Telegram Group",
                    )
                ),
                send_message=AsyncMock(),
            ),
        )
        gateway = SimpleNamespace(
            get_group_info=AsyncMock(
                return_value={
                    "group_id": q_group_id,
                    "group_name": "Example OneBot Group",
                }
            ),
            send_group_message=AsyncMock(return_value=1),
        )
        bus = MessageBus()

        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.notice.message_bus", bus),
        ):
            await self.handler.unbind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        assert await self.sql.get_q_group(tg_chat_id) is None
        context.bot.get_chat.assert_awaited_once_with(tg_chat_id)
        telegram_task = await bus.telegram_system_queue.get()
        target_group_task = await bus.telegram_system_queue.get()
        onebot_task = await bus.onebot_system_queue.get()
        assert isinstance(telegram_task, SendTask)
        assert isinstance(target_group_task, SendTask)
        assert isinstance(onebot_task, SendTask)
        await telegram_task.send()
        await target_group_task.send()
        await onebot_task.send()
        message.reply_text.assert_awaited_once_with("已解绑群 Example OneBot Group")
        context.bot.send_message.assert_awaited_once_with(
            chat_id=tg_chat_id,
            text="已解绑群 Example OneBot Group",
        )
        assert gateway.send_group_message.await_args.kwargs["message"][0]["data"]["text"] == (
            "已解绑群 Example Telegram Group"
        )

    async def test_unbind_from_private_chat_reports_missing_mapping(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=900_001, type="private"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(
            args=["123456789"],
            bot=SimpleNamespace(get_chat=AsyncMock()),
        )

        with patch("src.tgbot.handlers.sql", self.sql):
            await self.handler.unbind(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

        message.reply_text.assert_awaited_once_with("未找到该群聊的绑定关系")
        context.bot.get_chat.assert_not_awaited()
