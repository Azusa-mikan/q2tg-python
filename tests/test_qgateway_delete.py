import asyncio
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from fastapi import WebSocket

from src.qbot import QGateway


class QGatewayDeleteTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertEqual(request["action"], "delete_msg")
        self.assertEqual(request["params"], {"message_id": 123})

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

        self.assertEqual(request["action"], "get_group_member_info")
        self.assertEqual(request["params"], {"group_id": 123, "user_id": 456})
        self.assertNotIn("no_cache", request["params"])
        self.assertEqual(member["card"], "Group Card")

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

        self.assertEqual(request["action"], "get_group_member_info")
        self.assertEqual(
            request["params"],
            {"group_id": 123, "user_id": 456, "no_cache": True},
        )

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

        self.assertEqual(request["action"], "get_group_info")
        self.assertEqual(
            request["params"],
            {"group_id": 123_456_789, "no_cache": True},
        )
        self.assertEqual(group["group_name"], "Example OneBot Group")

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

        self.assertEqual(request["action"], "get_group_list")
        self.assertEqual(request["params"], {"no_cache": True})
        self.assertEqual(groups[0]["group_id"], 123_456_789)


if __name__ == "__main__":
    unittest.main()
