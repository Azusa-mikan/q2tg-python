import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import Application

from src.tgbot import BOT_COMMANDS, TGBot


class TestTGBotLifecycle:
    def test_proxy_is_applied_to_bot_api_and_polling(self) -> None:
        builder = MagicMock()
        builder.token.return_value = builder
        builder.media_write_timeout.return_value = builder
        builder.read_timeout.return_value = builder
        builder.proxy.return_value = builder
        builder.get_updates_proxy.return_value = builder
        built_app = MagicMock()
        builder.build.return_value = built_app
        bot = TGBot.__new__(TGBot)
        bot.handlers = MagicMock()

        with patch("src.tgbot.ApplicationBuilder", return_value=builder):
            result = bot._build_app(
                "token",
                proxy_url="http://127.0.0.1:8080",
            )

        assert result is built_app
        builder.media_write_timeout.assert_called_once_with(120)
        builder.read_timeout.assert_called_once_with(60)
        builder.proxy.assert_called_once_with("http://127.0.0.1:8080")
        builder.get_updates_proxy.assert_called_once_with("http://127.0.0.1:8080")

    @pytest.mark.asyncio
    async def test_only_first_start_drops_pending_updates(self) -> None:
        bot = TGBot.__new__(TGBot)
        updater = SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock())
        bot.app = cast(
            Application[Any, Any, Any, Any, Any, Any],
            SimpleNamespace(
            updater=updater,
            bot=SimpleNamespace(set_my_commands=AsyncMock()),
            initialize=AsyncMock(),
                start=AsyncMock(),
                stop=AsyncMock(),
                shutdown=AsyncMock(),
            ),
        )
        bot._lifecycle_lock = asyncio.Lock()
        bot._initialized = False
        bot._polling = False
        bot._running = False
        bot._started_once = False
        bot._commands_registered = False

        await bot.run()
        await bot.shutdown()
        await bot.run()

        assert [
            call.kwargs["drop_pending_updates"]
            for call in updater.start_polling.await_args_list
        ] == [True, False]
        assert bot.app.bot.set_my_commands.await_count == 1
        bot.app.bot.set_my_commands.assert_awaited_with(BOT_COMMANDS)
        await bot.shutdown()
