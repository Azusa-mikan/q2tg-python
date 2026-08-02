"""Telegraph 账号与页面客户端。"""

import asyncio
from typing import Any, cast

import httpx
from telegraph.aio import Telegraph

from src.config import config
from src.sql import Sql, sql

TELEGRAPH_TOKEN_KEY = "telegraph_access_token"


class TelegraphClient:
    """惰性创建 Telegraph 账号，并将管理 token 持久化到数据库。"""

    def __init__(
        self,
        database: Sql = sql,
        *,
        proxy_url: str | None = config.tgbot_proxy_url,
    ) -> None:
        self._database = database
        self._proxy_url = proxy_url
        self._client: Telegraph | None = None
        self._initialization_lock = asyncio.Lock()

    async def create_page(self, title: str, content: list[dict[str, Any]]) -> str:
        client = await self._get_client()
        page = await client.create_page(title, content=content)
        url = page.get("url")
        if not isinstance(url, str) or not url:
            raise RuntimeError(f"Telegraph 创建页面响应缺少 URL: {page!r}")
        return url

    async def close(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        await cast(Any, client)._telegraph.session.aclose()

    async def _get_client(self) -> Telegraph:
        if self._client is not None:
            return self._client
        async with self._initialization_lock:
            if self._client is not None:
                return self._client
            token = await self._database.get_setting(TELEGRAPH_TOKEN_KEY)
            client = Telegraph(access_token=token)
            telegraph_api = cast(Any, client)._telegraph
            default_session = telegraph_api.session
            telegraph_api.session = httpx.AsyncClient(
                proxy=self._proxy_url,
                trust_env=False,
            )
            await default_session.aclose()
            if token is None:
                try:
                    account = await client.create_account(
                        short_name="q2tg",
                        author_name="q2tg",
                    )
                    token = account.get("access_token")
                    if not isinstance(token, str) or not token:
                        raise RuntimeError(
                            "Telegraph 创建账号响应缺少 access_token: "
                            f"{account!r}"
                        )
                    await self._database.set_setting(TELEGRAPH_TOKEN_KEY, token)
                except BaseException:
                    await cast(Any, client)._telegraph.session.aclose()
                    raise
            self._client = client
            return client


telegraph_client = TelegraphClient()
