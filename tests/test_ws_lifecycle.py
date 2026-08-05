import asyncio
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

    async def test_non_object_json_is_ignored_without_dispatch(self) -> None:
        websocket = FakeWebSocket()
        websocket.receive_json = AsyncMock(
            side_effect=[None, WebSocketDisconnect()]
        )

        with (
            patch("src.ws.httpx.AsyncClient", side_effect=[AsyncMock(), AsyncMock()]),
            patch("src.ws.message_bus.consume", new_callable=AsyncMock),
            patch("src.ws.message_bus.stop_consumer", new_callable=AsyncMock),
            patch("src.ws.tgbot.run", new_callable=AsyncMock),
            patch("src.ws.tgbot.stop", new_callable=AsyncMock),
            patch("src.ws.tgbot.shutdown", new_callable=AsyncMock),
            patch("src.ws.q_gateway.resolve_response") as resolve,
            patch("src.ws.receive_onebot_event", new_callable=AsyncMock) as receive,
        ):
            await snowluma_ws(cast(WebSocket, websocket))

        resolve.assert_not_called()
        receive.assert_not_awaited()

    async def test_connection_cancellation_waits_for_resource_cleanup(self) -> None:
        websocket = FakeWebSocket()
        onebot_download_client = AsyncMock()
        telegram_download_client = AsyncMock()
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()

        async def stop_bot() -> None:
            stop_started.set()
            await release_stop.wait()

        with (
            patch(
                "src.ws.httpx.AsyncClient",
                side_effect=[onebot_download_client, telegram_download_client],
            ),
            patch("src.ws.message_bus.consume", new_callable=AsyncMock),
            patch("src.ws.message_bus.stop_consumer", new_callable=AsyncMock),
            patch("src.ws.tgbot.run", new_callable=AsyncMock),
            patch("src.ws.tgbot.stop", side_effect=stop_bot),
            patch("src.ws.tgbot.shutdown", new_callable=AsyncMock) as shutdown,
        ):
            connection = asyncio.create_task(snowluma_ws(cast(WebSocket, websocket)))
            await stop_started.wait()
            connection.cancel()
            await asyncio.sleep(0)
            assert not connection.done()

            release_stop.set()
            with pytest.raises(asyncio.CancelledError):
                await connection

        shutdown.assert_awaited_once_with()
        onebot_download_client.aclose.assert_awaited_once_with()
        telegram_download_client.aclose.assert_awaited_once_with()
