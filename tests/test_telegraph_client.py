from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from src.sql import Sql
from src.telegraph_client import TELEGRAPH_TOKEN_KEY, TelegraphClient


@pytest.mark.asyncio
class TestTelegraphClient:
    async def test_new_account_token_is_saved_and_reused(self) -> None:
        database = SimpleNamespace(
            get_setting=AsyncMock(return_value=None),
            set_setting=AsyncMock(),
        )
        default_session = SimpleNamespace(aclose=AsyncMock())
        api = SimpleNamespace(
            _telegraph=SimpleNamespace(session=default_session),
            create_account=AsyncMock(return_value={"access_token": "fixed-test-token"}),
            create_page=AsyncMock(return_value={"url": "https://telegra.ph/page"}),
        )
        configured_session = SimpleNamespace(aclose=AsyncMock())

        with (
            patch("src.telegraph_client.Telegraph", return_value=api) as constructor,
            patch(
                "src.telegraph_client.httpx.AsyncClient",
                return_value=configured_session,
            ) as session_constructor,
        ):
            client = TelegraphClient(
                cast(Sql, database),
                proxy_url="socks5://proxy.example:1080",
            )
            first = await client.create_page("标题一", [])
            second = await client.create_page("标题二", [])

        assert first == "https://telegra.ph/page"
        assert second == "https://telegra.ph/page"
        constructor.assert_called_once_with(access_token=None)
        session_constructor.assert_called_once_with(
            proxy="socks5://proxy.example:1080",
            trust_env=False,
        )
        default_session.aclose.assert_awaited_once_with()
        assert api._telegraph.session is configured_session
        api.create_account.assert_awaited_once_with(
            short_name="q2tg",
            author_name="q2tg",
        )
        database.set_setting.assert_awaited_once_with(
            TELEGRAPH_TOKEN_KEY,
            "fixed-test-token",
        )

    async def test_existing_token_skips_account_creation(self) -> None:
        database = SimpleNamespace(
            get_setting=AsyncMock(return_value="stored-test-token"),
            set_setting=AsyncMock(),
        )
        default_session = SimpleNamespace(aclose=AsyncMock())
        api = SimpleNamespace(
            _telegraph=SimpleNamespace(session=default_session),
            create_account=AsyncMock(),
            create_page=AsyncMock(return_value={"url": "https://telegra.ph/page"}),
        )

        with (
            patch("src.telegraph_client.Telegraph", return_value=api) as constructor,
            patch("src.telegraph_client.httpx.AsyncClient") as session_constructor,
        ):
            client = TelegraphClient(cast(Sql, database), proxy_url=None)
            await client.create_page("标题", [])

        constructor.assert_called_once_with(access_token="stored-test-token")
        session_constructor.assert_called_once_with(proxy=None, trust_env=False)
        default_session.aclose.assert_awaited_once_with()
        api.create_account.assert_not_awaited()
        database.set_setting.assert_not_awaited()
