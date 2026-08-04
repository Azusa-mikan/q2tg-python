from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call, patch

import pytest
import pytest_asyncio
from telegram import ChatMember, Update
from telegram.ext import ContextTypes

from src.messages import OneBotConnectionError
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


@pytest.mark.asyncio
class TestUnpin:
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self):
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "unpin.sqlite3")
        await self.sql.load()
        await self.sql.bind_group(123, -100_123)
        await self.sql.set_message_mapping(
            q_group_id=123,
            q_message_ids=(-1_001, -1_002),
            tg_chat_id=-100_123,
            tg_message_ids=(201, 202),
        )
        self.handler = TGhandlers()
        try:
            yield
        finally:
            await self.sql.close()
            self.directory.cleanup()

    async def _unpin(
        self,
        *,
        status: str,
        reply: bool = True,
        action_error: Exception | None = None,
    ):
        command = SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=201) if reply else None,
            reply_text=AsyncMock(),
            delete=AsyncMock(),
        )
        update = cast(
            Update,
            SimpleNamespace(
                effective_message=command,
                effective_chat=SimpleNamespace(id=-100_123, type="supergroup"),
                effective_user=SimpleNamespace(id=801),
            ),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
            unpin_chat_message=AsyncMock(),
        )
        context = cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(bot=bot))
        gateway = SimpleNamespace(
            delete_essence_message=AsyncMock(side_effect=action_error),
        )
        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
        ):
            await self.handler.unpin(update, context)
        return command, bot, gateway

    async def test_admin_unpins_all_mapped_messages_on_both_sides(self) -> None:
        command, bot, gateway = await self._unpin(
            status=ChatMember.ADMINISTRATOR,
        )

        assert gateway.delete_essence_message.await_args_list == [
            call(-1_001),
            call(-1_002),
        ]
        assert bot.unpin_chat_message.await_args_list == [
            call(chat_id=-100_123, message_id=201),
            call(chat_id=-100_123, message_id=202),
        ]
        command.reply_text.assert_not_awaited()
        command.delete.assert_awaited_once_with()

    async def test_member_cannot_unpin(self) -> None:
        command, bot, gateway = await self._unpin(status=ChatMember.MEMBER)

        gateway.delete_essence_message.assert_not_awaited()
        bot.unpin_chat_message.assert_not_awaited()
        command.reply_text.assert_awaited_once_with("只有群聊管理员可以取消置顶")

    async def test_command_requires_a_reply(self) -> None:
        command, bot, gateway = await self._unpin(
            status=ChatMember.ADMINISTRATOR,
            reply=False,
        )

        bot.get_chat_member.assert_not_awaited()
        gateway.delete_essence_message.assert_not_awaited()
        command.reply_text.assert_awaited_once_with(
            "请回复需要取消置顶的消息后使用 /unpin"
        )

    async def test_onebot_failure_keeps_telegram_pins(self) -> None:
        command, bot, gateway = await self._unpin(
            status=ChatMember.OWNER,
            action_error=RuntimeError("permission denied"),
        )

        assert gateway.delete_essence_message.await_count == 2
        bot.unpin_chat_message.assert_not_awaited()
        command.delete.assert_not_awaited()
        command.reply_text.assert_awaited_once_with(
            "OneBot 取消精华失败，机器人权限可能不足"
        )

    async def test_onebot_disconnect_keeps_telegram_pins(self) -> None:
        command, bot, gateway = await self._unpin(
            status=ChatMember.OWNER,
            action_error=OneBotConnectionError("disconnected"),
        )

        gateway.delete_essence_message.assert_awaited_once_with(-1_001)
        bot.unpin_chat_message.assert_not_awaited()
        command.delete.assert_not_awaited()
        command.reply_text.assert_awaited_once_with("OneBot 连接已断开，请稍后重试")
