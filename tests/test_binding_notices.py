import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from telegram import Update
from telegram.ext import ContextTypes

from src.bus import MessageBus
from src.config import config
from src.messages import SendTask
from src.sql import Sql
from src.tgbot.handlers import TGhandlers


class BindingNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.sql = Sql(Path(self.directory.name) / "q2tg.db")
        await self.sql.load()
        self.handler = TGhandlers()

    async def asyncTearDown(self) -> None:
        await self.sql.close()
        self.directory.cleanup()

    async def test_bind_and_unbind_send_the_same_notice_to_both_sides(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=-456, type="supergroup"),
            effective_user=SimpleNamespace(id=config.tgbot_admin),
        )
        context = SimpleNamespace(args=["123"])
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=1))
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

        self.assertEqual(
            [call.args[0] for call in message.reply_text.await_args_list],
            [
                "已绑定 Telegram 群与 Onebot 群 123",
                "已解除 Telegram 群与 Onebot 群 123 的绑定",
            ],
        )
        self.assertEqual(
            [call.kwargs["message"][0]["data"]["text"] for call in gateway.send_group_message.await_args_list],
            [
                "已绑定 Telegram 群与 Onebot 群 123",
                "已解除 Telegram 群与 Onebot 群 123 的绑定",
            ],
        )


if __name__ == "__main__":
    unittest.main()
