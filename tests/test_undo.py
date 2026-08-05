from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from telegram import ChatMember, Update
from telegram.ext import ContextTypes

from src.messages import OneBotConnectionError, OneBotResultUnknownError
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


@pytest.mark.asyncio
class TestUndo:
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "cache.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -456)
        self.handler = TGhandlers()
        try:
            yield
        finally:
            await self.sql.close()
            self.directory.cleanup()

    async def _undo(
        self,
        *,
        user_id: int,
        original_user_id: int,
        status: str,
        delete_error: Exception | None = None,
    ):
        await self.sql.set_message_mapping(
            q_group_id=123,
            q_message_ids=(99, 100),
            tg_chat_id=-456,
            tg_message_ids=(200, 201),
            tg_user_id=original_user_id,
        )
        command = SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=200),
            reply_text=AsyncMock(),
            delete=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=command,
            effective_chat=SimpleNamespace(id=-456, type="supergroup"),
            effective_user=SimpleNamespace(id=user_id),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
            delete_messages=AsyncMock(),
        )
        context = SimpleNamespace(bot=bot)
        gateway = SimpleNamespace(
            delete_message=AsyncMock(side_effect=delete_error),
        )
        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
        ):
            await self.handler.undo(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )
        return command, bot, gateway

    async def test_member_can_undo_own_message(self) -> None:
        command, bot, gateway = await self._undo(
            user_id=10,
            original_user_id=10,
            status=ChatMember.MEMBER,
        )
        assert [call.args[0] for call in gateway.delete_message.await_args_list] == [
            99,
            100,
        ]
        bot.delete_messages.assert_awaited_once_with(chat_id=-456, message_ids=(200, 201))
        command.reply_text.assert_not_awaited()
        command.delete.assert_awaited_once()

    async def test_member_cannot_undo_other_users_message(self) -> None:
        command, bot, gateway = await self._undo(
            user_id=11,
            original_user_id=10,
            status=ChatMember.MEMBER,
        )
        gateway.delete_message.assert_not_awaited()
        bot.delete_messages.assert_not_awaited()
        command.reply_text.assert_awaited_with("非群聊管理员只能撤回自己的消息")

    async def test_group_admin_can_undo_any_message(self) -> None:
        _, bot, gateway = await self._undo(
            user_id=11,
            original_user_id=10,
            status=ChatMember.ADMINISTRATOR,
        )
        assert gateway.delete_message.await_count == 2
        bot.delete_messages.assert_awaited_once()

    async def test_onebot_rejection_keeps_command_and_shows_error(self) -> None:
        command, bot, gateway = await self._undo(
            user_id=10,
            original_user_id=10,
            status=ChatMember.MEMBER,
            delete_error=RuntimeError("too old"),
        )
        assert gateway.delete_message.await_count == 2
        bot.delete_messages.assert_not_awaited()
        command.delete.assert_not_awaited()
        command.reply_text.assert_awaited_with(
            "OneBot 撤回失败，消息可能超过两分钟或机器人权限不足"
        )

    @pytest.mark.parametrize(
        "delete_error",
        [
            OneBotConnectionError("disconnected"),
            OneBotResultUnknownError("result unknown"),
        ],
    )
    async def test_onebot_disconnect_keeps_command_and_shows_error(
        self,
        delete_error: Exception,
    ) -> None:
        command, bot, gateway = await self._undo(
            user_id=10,
            original_user_id=10,
            status=ChatMember.MEMBER,
            delete_error=delete_error,
        )
        gateway.delete_message.assert_awaited_once_with(99)
        bot.delete_messages.assert_not_awaited()
        command.delete.assert_not_awaited()
        command.reply_text.assert_awaited_once_with("OneBot 连接已断开，请稍后重试")
