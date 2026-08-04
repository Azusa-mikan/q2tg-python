from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from src.messages import SendLane, SendTarget
from src.ws import snowluma_ws


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        raise WebSocketDisconnect()


@pytest.mark.asyncio
class TestWebSocketLifecycle:
    async def test_ptb_runs_only_during_snowluma_connection(self) -> None:
        websocket = FakeWebSocket()
        onebot_download_client = AsyncMock()
        telegram_download_client = AsyncMock()

        with (
            patch(
                "src.ws.httpx.AsyncClient",
                side_effect=[onebot_download_client, telegram_download_client],
            ) as client,
            patch("src.ws.config.onebot_proxy_url", "http://127.0.0.1:8080"),
            patch("src.ws.config.tgbot_proxy_url", "socks5://127.0.0.1:1080"),
            patch("src.ws.message_bus.consume", new_callable=AsyncMock) as consume,
            patch("src.ws.message_bus.stop_consumer", new_callable=AsyncMock) as stop_consumer,
            patch("src.ws.tgbot.run", new_callable=AsyncMock) as run,
            patch("src.ws.tgbot.stop", new_callable=AsyncMock) as stop,
            patch("src.ws.tgbot.shutdown", new_callable=AsyncMock) as shutdown,
        ):
            await snowluma_ws(cast(WebSocket, websocket))

        assert websocket.accepted
        run.assert_awaited_once_with()
        stop.assert_awaited_once_with()
        assert consume.call_count == 6
        for target in SendTarget:
            for lane in SendLane:
                consume.assert_any_call(target, lane)
        assert stop_consumer.await_count == 3
        for lane in SendLane:
            stop_consumer.assert_any_await(SendTarget.TELEGRAM, lane)
        shutdown.assert_awaited_once_with()
        assert client.call_count == 2
        assert client.call_args_list[0].kwargs["proxy"] == "http://127.0.0.1:8080"
        assert client.call_args_list[1].kwargs["proxy"] == "socks5://127.0.0.1:1080"
        onebot_download_client.aclose.assert_awaited_once_with()
        telegram_download_client.aclose.assert_awaited_once_with()
