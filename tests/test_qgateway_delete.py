import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from src.messages import OneBotResultUnknownError
from src.qbot import QGateway


@pytest.mark.asyncio
class TestQGatewayDelete:
    async def test_disconnect_after_action_send_reports_unknown_result(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.send_group_message(123, []))
        while not websocket.send_json.await_count:
            await asyncio.sleep(0)
        gateway.unbind(cast(WebSocket, websocket))

        with pytest.raises(OneBotResultUnknownError, match="执行结果未知"):
            await task

    async def test_disconnect_cancels_action_blocked_while_sending(self) -> None:
        send_started = asyncio.Event()

        async def send_json(data) -> None:
            send_started.set()
            await asyncio.Event().wait()

        websocket = SimpleNamespace(send_json=AsyncMock(side_effect=send_json))
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))
        action = asyncio.create_task(gateway.send_group_message(123, []))
        await send_started.wait()

        gateway.unbind(cast(WebSocket, websocket))

        with pytest.raises(OneBotResultUnknownError, match="执行结果未知"):
            await asyncio.wait_for(action, timeout=0.1)
        await asyncio.wait_for(gateway.wait_pending(), timeout=0.1)

    async def test_disconnect_unblocks_send_after_response_was_received(self) -> None:
        send_started = asyncio.Event()

        async def send_json(data) -> None:
            send_started.set()
            await asyncio.Event().wait()

        websocket = SimpleNamespace(send_json=AsyncMock(side_effect=send_json))
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))
        action = asyncio.create_task(gateway.send_group_message(123, []))
        await send_started.wait()
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 456},
                "echo": request["echo"],
            }
        )

        gateway.unbind(cast(WebSocket, websocket))

        assert await asyncio.wait_for(action, timeout=0.1) == 456
        await asyncio.wait_for(gateway.wait_pending(), timeout=0.1)

    async def test_delete_message_uses_delete_msg_action(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.delete_message(123))
        while not websocket.send_json.await_count:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": None,
                "echo": request["echo"],
            }
        )
        await task

        assert request["action"] == "delete_msg"
        assert request["params"] == {"message_id": 123}

    async def test_get_group_member_info_uses_default_cache(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.get_group_member_info(123, 456))
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "group_id": 123,
                    "user_id": 456,
                    "nickname": "Nickname",
                    "card": "Group Card",
                },
                "echo": request["echo"],
            }
        )
        member = await task

        assert request["action"] == "get_group_member_info"
        assert request["params"] == {"group_id": 123, "user_id": 456}
        assert "no_cache" not in request["params"]
        assert member["card"] == "Group Card"

    async def test_get_group_member_info_can_bypass_cache(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(
            gateway.get_group_member_info(123, 456, no_cache=True)
        )
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"group_id": 123, "user_id": 456, "nickname": "User"},
                "echo": request["echo"],
            }
        )
        await task

        assert request["action"] == "get_group_member_info"
        assert request["params"] == {
            "group_id": 123,
            "user_id": 456,
            "no_cache": True,
        }

    async def test_get_group_member_list_always_bypasses_cache(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.get_group_member_list(810_001))
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": [
                    {
                        "group_id": 810_001,
                        "user_id": 810_002,
                        "nickname": "Example Member",
                        "card": "Sample Card",
                    }
                ],
                "echo": request["echo"],
            }
        )
        members = await task

        assert request["action"] == "get_group_member_list"
        assert request["params"] == {"group_id": 810_001, "no_cache": True}
        assert members[0]["user_id"] == 810_002

    async def test_get_stranger_info_uses_only_user_id(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.get_stranger_info(810_003))
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 810_003, "nickname": "Example Stranger"},
                "echo": request["echo"],
            }
        )
        stranger = await task

        assert request["action"] == "get_stranger_info"
        assert request["params"] == {"user_id": 810_003}
        assert stranger["nickname"] == "Example Stranger"

    async def test_get_group_info_uses_group_id(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.get_group_info(123_456_789))
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "group_id": 123_456_789,
                    "group_name": "Example OneBot Group",
                },
                "echo": request["echo"],
            }
        )
        group = await task

        assert request["action"] == "get_group_info"
        assert request["params"] == {"group_id": 123_456_789, "no_cache": True}
        assert group["group_name"] == "Example OneBot Group"

    async def test_get_group_list_always_bypasses_cache(self) -> None:
        websocket = SimpleNamespace(send_json=AsyncMock())
        gateway = QGateway()
        gateway.bind(cast(WebSocket, websocket))

        task = asyncio.create_task(gateway.get_group_list())
        while websocket.send_json.await_count < 1:
            await asyncio.sleep(0)
        request = websocket.send_json.await_args.args[0]
        gateway.resolve_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": [
                    {
                        "group_id": 123_456_789,
                        "group_name": "Example OneBot Group",
                    }
                ],
                "echo": request["echo"],
            }
        )
        groups = await task

        assert request["action"] == "get_group_list"
        assert request["params"] == {"no_cache": True}
        assert groups[0]["group_id"] == 123_456_789
