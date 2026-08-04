import unittest
from functools import partial
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from telegram import Message, Update
from telegram.ext import ContextTypes

from src.bus import MessageBus
from src.messages import SendTask
from src.tgbot.handlers import TGhandlers


class TelegramBotForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_bot_text_requires_bot_forward_switch(self) -> None:
        handler = TGhandlers()
        bus = MessageBus()
        message = SimpleNamespace(
            message_id=700001,
            chat_id=-700002,
            from_user=SimpleNamespace(
                id=700003,
                full_name="Example Helper Bot",
                is_bot=True,
            ),
            text="example message",
            forward_origin=None,
            reply_to_message=None,
        )
        update = cast(Update, SimpleNamespace(effective_message=message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=700004)),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.tgbot.handlers.sql.get_bot_forward_enabled",
                new_callable=AsyncMock,
                side_effect=[False, True],
            ),
            patch("src.tgbot.handlers.message_bus", bus),
        ):
            await handler.receive_message(update, context)
            self.assertTrue(bus.onebot_queue.empty())

            await handler.receive_message(update, context)
            self.assertEqual(bus.onebot_queue.qsize(), 1)
            task = await bus.onebot_queue.get()
            self.assertIsInstance(task, SendTask)
            assert isinstance(task, SendTask)
            self.assertIsInstance(task.send, partial)
            assert isinstance(task.send, partial)
            self.assertTrue(task.send.args[0].bot_forward_required)
            bus.onebot_queue.task_done()

    async def test_current_bot_message_is_never_forwarded(self) -> None:
        handler = TGhandlers()
        message = cast(
            Message,
            SimpleNamespace(
                chat_id=-700002,
                from_user=SimpleNamespace(id=700004, is_bot=True),
            ),
        )

        with patch(
            "src.tgbot.handlers.sql.get_bot_forward_enabled",
            new_callable=AsyncMock,
        ) as get_enabled:
            self.assertFalse(await handler._can_forward_sender(message, 700004))

        get_enabled.assert_not_awaited()

    async def test_user_command_requires_bot_forward_switch(self) -> None:
        handler = TGhandlers()
        bus = MessageBus()
        message = SimpleNamespace(
            message_id=700005,
            chat_id=-700002,
            from_user=SimpleNamespace(
                id=700006,
                full_name="Example User",
                is_bot=False,
            ),
            text="/new@ExampleAssistantBot",
            forward_origin=None,
            reply_to_message=None,
        )
        update = cast(Update, SimpleNamespace(effective_message=message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=700004)),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.tgbot.handlers.sql.get_bot_forward_enabled",
                new_callable=AsyncMock,
                side_effect=[False, True],
            ),
            patch("src.tgbot.handlers.message_bus", bus),
        ):
            await handler.receive_command(update, context)
            self.assertTrue(bus.onebot_queue.empty())

            await handler.receive_command(update, context)
            self.assertEqual(bus.onebot_queue.qsize(), 1)
            task = await bus.onebot_queue.get()
            self.assertIsInstance(task, SendTask)
            assert isinstance(task, SendTask)
            self.assertIsInstance(task.send, partial)
            assert isinstance(task.send, partial)
            self.assertTrue(task.send.args[0].bot_forward_required)
            bus.onebot_queue.task_done()

    async def test_other_bot_media_requires_bot_forward_switch(self) -> None:
        handler = TGhandlers()
        video = SimpleNamespace(file_size=7, get_file=AsyncMock())
        message = cast(
            Message,
            SimpleNamespace(
                message_id=700001,
                chat_id=-700002,
                from_user=SimpleNamespace(
                    id=700003,
                    full_name="Example Helper Bot",
                    is_bot=True,
                ),
                video=video,
                photo=(),
            ),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.tgbot.handlers.sql.get_bot_forward_enabled",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await handler._enqueue_media([message], bot_id=700004)

        video.get_file.assert_not_awaited()

    async def test_anonymous_admin_is_forwarded_without_bot_forward_switch(self) -> None:
        handler = TGhandlers()
        bus = MessageBus()
        message = SimpleNamespace(
            message_id=700007,
            chat_id=-700002,
            from_user=SimpleNamespace(
                id=1087968824,
                full_name="Group",
                is_bot=True,
            ),
            sender_chat=SimpleNamespace(id=-700002),
            text="anonymous admin message",
            forward_origin=None,
            reply_to_message=None,
        )
        update = cast(Update, SimpleNamespace(effective_message=message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=700004)),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.tgbot.handlers.sql.get_bot_forward_enabled",
                new_callable=AsyncMock,
                return_value=False,
            ) as get_bot_forward,
            patch("src.tgbot.handlers.message_bus", bus),
        ):
            await handler.receive_message(update, context)

        self.assertEqual(bus.onebot_queue.qsize(), 1)
        task = await bus.onebot_queue.get()
        assert isinstance(task, SendTask)
        assert isinstance(task.send, partial)
        forwarded = task.send.args[0]
        self.assertEqual(forwarded.sender_name, "Group")
        self.assertFalse(forwarded.bot_forward_required)
        bus.onebot_queue.task_done()
        get_bot_forward.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
