import asyncio
import re
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from src.messages import TelegraphPageRef
from src.onebot_forward import (
    MAX_FORWARD_MESSAGES,
    UNSUPPORTED_FORWARD,
    ForwardPageBuilder,
)
from src.qbot import QGateway
from src.telegraph_client import TelegraphClient


def forward_message(forward_id: str, *, message_type: str = "group") -> dict:
    return {
        "message_type": message_type,
        "sender": {"nickname": "测试用户"},
        "message": [{"type": "forward", "data": {"id": forward_id}}],
    }


@pytest.mark.asyncio
class TestOneBotForwardPage:
    async def test_private_title_has_sixteen_random_characters(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "private",
                        "sender": {"nickname": "测试用户"},
                        "message": [{"type": "text", "data": {"text": "你好"}}],
                    }
                ]
            )
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        page = await builder.create("root")
        assert page.url == "https://telegra.ph/x"
        assert re.fullmatch(r"私聊的聊天记录 - [0-9a-f]{16}", page.title)

        title, content = telegraph.create_page.await_args.args
        assert re.fullmatch(r"私聊的聊天记录 - [0-9a-f]{16}", title)
        assert content[0] == {"tag": "hr"}
        assert {"tag": "blockquote", "children": ["你好"]} in content

    async def test_missing_sender_name_uses_onebot_user(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "group",
                        "sender": {"nickname": "", "card": ""},
                        "message": [{"type": "text", "data": {"text": "你好"}}],
                    }
                ]
            )
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        content = telegraph.create_page.await_args.args[1]
        assert {"tag": "p", "children": ["OneBot 用户:"]} in content

    async def test_at_uses_stranger_nickname_and_never_shows_id(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "group",
                        "sender": {"nickname": "Example Sender"},
                        "message": [
                            {"type": "at", "data": {"qq": "820001"}},
                            {"type": "at", "data": {"qq": 820001}},
                        ],
                    }
                ]
            ),
            get_stranger_info=AsyncMock(
                return_value={"user_id": 820001, "nickname": "Example Target"}
            ),
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        content = telegraph.create_page.await_args.args[1]
        assert {
            "tag": "blockquote",
            "children": ["@Example Target@Example Target"],
        } in content
        assert "820001" not in repr(content)
        gateway.get_stranger_info.assert_awaited_once_with(820001)

    async def test_at_lookup_failure_reuses_onebot_user_name(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "group",
                        "sender": {"nickname": "Example Sender"},
                        "message": [{"type": "at", "data": {"qq": "820002"}}],
                    }
                ]
            ),
            get_stranger_info=AsyncMock(side_effect=RuntimeError("failed")),
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        content = telegraph.create_page.await_args.args[1]
        assert {"tag": "blockquote", "children": ["@OneBot 用户"]} in content
        assert "820002" not in repr(content)

    async def test_at_lookups_run_with_bounded_concurrency(self) -> None:
        active = 0
        maximum = 0

        async def get_stranger_info(user_id: int) -> dict[str, object]:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return {"user_id": user_id, "nickname": f"Member {user_id}"}

        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "group",
                        "sender": {"nickname": "Example Sender"},
                        "message": [
                            {"type": "at", "data": {"qq": user_id}}
                            for user_id in range(820_100, 820_112)
                        ],
                    }
                ]
            ),
            get_stranger_info=AsyncMock(side_effect=get_stranger_info),
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        assert maximum > 1
        assert maximum <= 8

    async def test_at_lookup_skips_messages_beyond_page_limit(self) -> None:
        gateway = SimpleNamespace(get_stranger_info=AsyncMock())
        telegraph = SimpleNamespace()
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )
        builder.message_count = MAX_FORWARD_MESSAGES

        nodes = await builder._messages_to_nodes(
            [
                {
                    "sender": {"nickname": "Example Sender"},
                    "message": [{"type": "at", "data": {"qq": 820_200}}],
                }
            ],
            depth=1,
        )

        assert nodes == [builder._quote("[合并转发消息过多，后续内容已省略]")]
        gateway.get_stranger_info.assert_not_awaited()

    async def test_media_rejects_http_url_without_host(self) -> None:
        builder = ForwardPageBuilder(
            cast(QGateway, SimpleNamespace()),
            cast(TelegraphClient, SimpleNamespace()),
        )

        node = await builder._media_node("image", {"url": "https:///image.png"})

        assert node == builder._quote("[图片无法读取]")

    async def test_fourth_level_is_not_requested(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                side_effect=[
                    [forward_message("level-2")],
                    [forward_message("level-3")],
                    [forward_message("level-4")],
                ]
            )
        )
        telegraph = SimpleNamespace(
            create_page=AsyncMock(
                side_effect=[
                    "https://telegra.ph/level-3",
                    "https://telegra.ph/level-2",
                    "https://telegra.ph/root",
                ]
            )
        )
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        root_page = await builder.create("root")
        assert root_page.url == "https://telegra.ph/root"

        assert [
            call.args[0] for call in gateway.get_forward_messages.await_args_list
        ] == ["root", "level-2", "level-3"]
        assert telegraph.create_page.await_count == 3
        level_3_content = telegraph.create_page.await_args_list[0].args[1]
        level_2_content = telegraph.create_page.await_args_list[1].args[1]
        root_content = telegraph.create_page.await_args_list[2].args[1]
        assert UNSUPPORTED_FORWARD in repr(level_3_content)
        assert "level-4" not in repr(level_3_content)
        assert {
            "tag": "blockquote",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": "https://telegra.ph/level-3"},
                    "children": [
                        TelegraphPageRef(
                            title=telegraph.create_page.await_args_list[0].args[0],
                            url="https://telegra.ph/level-3",
                        ).title
                    ],
                }
            ],
        } in level_2_content
        assert "https://telegra.ph/level-2" in repr(root_content)

    async def test_repeated_nested_forward_reuses_page(self) -> None:
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                side_effect=[
                    [
                        forward_message("shared"),
                        forward_message("shared"),
                    ],
                    [
                        {
                            "message_type": "group",
                            "sender": {"nickname": "测试用户"},
                            "message": [
                                {"type": "text", "data": {"text": "子页面"}}
                            ],
                        }
                    ],
                ]
            )
        )
        telegraph = SimpleNamespace(
            create_page=AsyncMock(
                side_effect=[
                    "https://telegra.ph/shared",
                    "https://telegra.ph/root",
                ]
            )
        )
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        assert [
            call.args[0] for call in gateway.get_forward_messages.await_args_list
        ] == ["root", "shared"]
        root_content = telegraph.create_page.await_args_list[1].args[1]
        assert repr(root_content).count("https://telegra.ph/shared") == 2

    async def test_image_uses_original_media_url(self) -> None:
        media_url = "https://media.example/image"
        gateway = SimpleNamespace(
            get_forward_messages=AsyncMock(
                return_value=[
                    {
                        "message_type": "group",
                        "sender": {"nickname": "测试用户"},
                        "message": [
                            {
                                "type": "image",
                                "data": {
                                    "url": media_url,
                                    "file": "photo.jpg",
                                },
                            }
                        ],
                    }
                ]
            )
        )
        telegraph = SimpleNamespace(create_page=AsyncMock(return_value="https://telegra.ph/x"))
        builder = ForwardPageBuilder(
            cast(QGateway, gateway),
            cast(TelegraphClient, telegraph),
        )

        await builder.create("root")

        content = telegraph.create_page.await_args.args[1]
        assert {"tag": "img", "attrs": {"src": media_url}} in content
