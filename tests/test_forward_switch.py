import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from telegram import ChatMember, Update
from telegram.ext import ContextTypes

from src.bus import MessageBus
from src.messages import SendTask
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


class ForwardSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "q2tg.db")
        await self.sql.load()
        await self.sql.bind_group(123, -456)
        self.handler = TGhandlers()

    async def asyncTearDown(self) -> None:
        await self.sql.close()
        self.directory.cleanup()

    async def _forward(self, *, args: list[str], status: str):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=-456, type="supergroup"),
            effective_user=SimpleNamespace(id=10),
        )
        context = SimpleNamespace(
            args=args,
            bot=SimpleNamespace(
                get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status))
            ),
        )
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=1))
        bus = MessageBus()
        with (
            patch("src.tgbot.handlers.sql", self.sql),
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch("src.notice.message_bus", bus),
        ):
            await self.handler.forward(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )
        for queue in (bus.telegram_queue, bus.onebot_queue):
            while not queue.empty():
                task = await queue.get()
                try:
                    assert isinstance(task, SendTask)
                    await task.send()
                finally:
                    queue.task_done()
        return message, gateway

    async def _id_show(self, *, args: list[str], status: str):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=-456, type="supergroup"),
            effective_user=SimpleNamespace(id=10),
        )
        context = SimpleNamespace(
            args=args,
            bot=SimpleNamespace(
                get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status))
            ),
        )
        with patch("src.tgbot.handlers.sql", self.sql):
            await self.handler.id_show(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )
        return message

    async def test_group_admin_can_disable_and_enable_forwarding(self) -> None:
        message, gateway = await self._forward(args=["off"], status=ChatMember.ADMINISTRATOR)
        self.assertFalse(await self.sql.get_tg_forward_enabled(-456))
        message.reply_text.assert_awaited_once_with("Telegram → Onebot 转发已关闭")
        gateway.send_group_message.assert_awaited_once_with(
            group_id=123,
            message=[{"type": "text", "data": {"text": "Telegram → Onebot 转发已关闭"}}],
        )

        message, gateway = await self._forward(args=["on"], status=ChatMember.OWNER)
        self.assertTrue(await self.sql.get_tg_forward_enabled(-456))
        message.reply_text.assert_awaited_once_with("Telegram → Onebot 转发已开启")
        gateway.send_group_message.assert_awaited_once()

    async def test_regular_member_cannot_change_forwarding(self) -> None:
        message, gateway = await self._forward(args=["off"], status=ChatMember.MEMBER)
        self.assertTrue(await self.sql.get_tg_forward_enabled(-456))
        message.reply_text.assert_awaited_once_with("只有群聊管理员可以设置转发开关")
        gateway.send_group_message.assert_not_awaited()

    async def test_group_admin_can_disable_and_query_id_show(self) -> None:
        self.assertTrue(await self.sql.get_id_show_enabled(-456))

        message = await self._id_show(args=["off"], status=ChatMember.ADMINISTRATOR)
        self.assertFalse(await self.sql.get_id_show_enabled(-456))
        message.reply_text.assert_awaited_once_with("Onebot 用户及 @ 对象 ID 显示已关闭")

        message = await self._id_show(args=[], status=ChatMember.OWNER)
        message.reply_text.assert_awaited_once_with("Onebot 用户及 @ 对象 ID 显示已关闭")

    async def test_regular_member_cannot_change_id_show(self) -> None:
        message = await self._id_show(args=["off"], status=ChatMember.MEMBER)
        self.assertTrue(await self.sql.get_id_show_enabled(-456))
        message.reply_text.assert_awaited_once_with("只有群聊管理员可以设置 ID 显示")


if __name__ == "__main__":
    unittest.main()
