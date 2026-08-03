import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from src.api import lifespan


class ApiLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_purger_does_not_skip_resource_shutdown(self) -> None:
        with (
            patch("src.api.sql.load", new_callable=AsyncMock) as load,
            patch("src.api.sql.close", new_callable=AsyncMock) as close,
            patch("src.api.media_cache.close") as close_media,
            patch("src.api.purge_cache", new_callable=AsyncMock) as purge,
            patch("src.lifecycle.baselog.exception"),
        ):
            purge.side_effect = RuntimeError("purger failed")
            async with lifespan(cast(FastAPI, SimpleNamespace())):
                await asyncio.sleep(0)

        load.assert_awaited_once_with()
        close.assert_awaited_once_with()
        close_media.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
