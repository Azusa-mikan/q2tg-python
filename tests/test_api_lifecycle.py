import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from src.api import lifespan, purge_cache


@pytest.mark.asyncio
class TestApiLifecycle:
    async def test_purger_continues_after_database_and_media_failures(self) -> None:
        with (
            patch(
                "src.api.asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=[None, None, asyncio.CancelledError],
            ),
            patch(
                "src.api.sql.purge_expired",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("database unavailable"), None],
            ) as purge_sql,
            patch(
                "src.api.media_cache.purge_expired",
                side_effect=[RuntimeError("cache failure"), None],
            ) as purge_media,
            patch("src.api.baselog.exception") as log_exception,
            pytest.raises(asyncio.CancelledError),
        ):
            await purge_cache()

        assert purge_sql.await_count == 2
        assert purge_media.call_count == 2
        assert log_exception.call_count == 2

    async def test_failed_purger_does_not_skip_resource_shutdown(self) -> None:
        with (
            patch("src.api.sql.load", new_callable=AsyncMock) as load,
            patch("src.api.sql.close", new_callable=AsyncMock) as close,
            patch("src.api.media_cache.close") as close_media,
            patch("src.api.mapping_outbox.load", new_callable=AsyncMock),
            patch("src.api.mapping_outbox.run", new_callable=AsyncMock),
            patch("src.api.mapping_outbox.close", new_callable=AsyncMock),
            patch("src.api.purge_cache", new_callable=AsyncMock) as purge,
            patch("src.lifecycle.baselog.exception"),
        ):
            purge.side_effect = RuntimeError("purger failed")
            async with lifespan(cast(FastAPI, SimpleNamespace())):
                await asyncio.sleep(0)

        load.assert_awaited_once_with()
        close.assert_awaited_once_with()
        close_media.assert_called_once_with()
