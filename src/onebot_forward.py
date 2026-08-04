"""将 OneBot 合并转发转换成 Telegraph 页面。"""

from __future__ import annotations

import asyncio
from secrets import token_hex
from typing import TYPE_CHECKING, Any

from src.face import render_onebot_face
from src.log import baselog
from src.messages import (
    ONEBOT_USER_NAME,
    TelegraphPageRef,
    is_http_url,
    onebot_user_id,
    onebot_user_name,
)
from src.telegraph_client import TelegraphClient

if TYPE_CHECKING:
    from src.qbot import QGateway

MAX_FORWARD_DEPTH = 3
MAX_FORWARD_MESSAGES = 500
MAX_STRANGER_LOOKUPS = 8
UNSUPPORTED_FORWARD = "[合并转发]该消息不支持查看"


class ForwardPageBuilder:
    """把每层 OneBot 合并转发分别创建为相互链接的 Telegraph 页面。"""

    def __init__(
        self,
        gateway: QGateway,
        telegraph: TelegraphClient,
    ) -> None:
        self.gateway = gateway
        self.telegraph = telegraph
        self.message_count = 0
        self.pages: dict[str, TelegraphPageRef] = {}
        self.active_ids: set[str] = set()
        self.stranger_names: dict[int, str | None] = {}
        self.stranger_lookup_slots = asyncio.Semaphore(MAX_STRANGER_LOOKUPS)

    async def create(self, forward_id: str) -> TelegraphPageRef:
        return await self._create_page(forward_id, depth=1)

    async def _create_page(self, forward_id: str, *, depth: int) -> TelegraphPageRef:
        cached_page = self.pages.get(forward_id)
        if cached_page is not None:
            return cached_page
        if forward_id in self.active_ids:
            raise RuntimeError("OneBot 合并转发存在循环引用")
        self.active_ids.add(forward_id)
        try:
            messages = await self.gateway.get_forward_messages(forward_id)
            title = self._title(messages)
            content: list[dict[str, Any]] = [{"tag": "hr"}]
            content.extend(await self._messages_to_nodes(messages, depth=depth))
            page_url = await self.telegraph.create_page(title, content)
        finally:
            self.active_ids.remove(forward_id)
        page = TelegraphPageRef(title=title, url=page_url)
        self.pages[forward_id] = page
        return page

    async def _messages_to_nodes(
        self,
        messages: list[dict[str, Any]],
        *,
        depth: int,
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        remaining = max(MAX_FORWARD_MESSAGES - self.message_count, 0)
        await self._cache_stranger_names(messages[:remaining])
        for message in messages:
            self.message_count += 1
            if self.message_count > MAX_FORWARD_MESSAGES:
                nodes.append(self._quote("[合并转发消息过多，后续内容已省略]"))
                break
            sender = message.get("sender")
            name = (
                onebot_user_name(sender) if isinstance(sender, dict) else None
            ) or ONEBOT_USER_NAME
            nodes.append(
                {
                    "tag": "p",
                    "children": [f"{name}:"],
                }
            )
            segments = message.get("message")
            if not isinstance(segments, list):
                nodes.append(self._quote("[消息内容无法读取]"))
                continue
            segment_nodes = await self._segments_to_nodes(segments, depth=depth)
            nodes.extend(segment_nodes or [self._quote("[空消息]")])
        return nodes

    async def _segments_to_nodes(
        self,
        segments: list[Any],
        *,
        depth: int,
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        text_parts: list[str] = []

        def flush_text() -> None:
            if text_parts:
                nodes.append(self._quote("".join(text_parts)))
                text_parts.clear()

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            if kind == "text":
                text = data.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif kind == "face":
                face_id = data.get("id")
                if face_id is not None:
                    text_parts.append(render_onebot_face(face_id))
            elif kind == "at":
                target = data.get("qq")
                if target == "all":
                    text_parts.append("@全体成员")
                    continue
                user_id = onebot_user_id(target)
                if user_id is None:
                    text_parts.append(f"@{ONEBOT_USER_NAME}")
                    continue
                name = await self._stranger_name(user_id)
                # Telegraph 页面永久隐藏数字 ID，不受群级 id_show 设置影响。
                text_parts.append(f"@{name or ONEBOT_USER_NAME}")
            elif kind in {"image", "video"}:
                flush_text()
                nodes.append(await self._media_node(kind, data))
            elif kind == "forward":
                flush_text()
                nested_id = data.get("id")
                if depth >= MAX_FORWARD_DEPTH or not isinstance(nested_id, str):
                    nodes.append(self._quote(UNSUPPORTED_FORWARD))
                    continue
                try:
                    nested_page = await self._create_page(nested_id, depth=depth + 1)
                except Exception:  # noqa: BLE001
                    baselog.exception("OneBot 内层合并转发页面创建失败")
                    baselog.warning(
                        "OneBot 内层合并转发使用占位内容: depth=%s",
                        depth,
                    )
                    nodes.append(self._quote(UNSUPPORTED_FORWARD))
                else:
                    nodes.append(self._forward_link(nested_page))
            elif kind == "record":
                text_parts.append("[语音]")
            elif kind == "file":
                filename = data.get("file")
                text_parts.append(
                    f"[文件：{filename}]" if isinstance(filename, str) else "[文件]"
                )
            else:
                text_parts.append(f"[{kind}]" if isinstance(kind, str) else "[消息]")
        flush_text()
        return nodes

    async def _stranger_name(self, user_id: int) -> str | None:
        if user_id in self.stranger_names:
            return self.stranger_names[user_id]
        async with self.stranger_lookup_slots:
            try:
                stranger = await self.gateway.get_stranger_info(user_id)
            except Exception:  # noqa: BLE001
                baselog.warning("OneBot 陌生人资料查询失败: user=%s", user_id)
                name = None
            else:
                nickname = stranger.get("nickname")
                name = (
                    nickname.strip()
                    if isinstance(nickname, str) and nickname.strip()
                    else None
                )
        self.stranger_names[user_id] = name
        return name

    async def _cache_stranger_names(self, messages: list[dict[str, Any]]) -> None:
        user_ids: set[int] = set()
        for message in messages:
            segments = message.get("message")
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "at":
                    continue
                data = segment.get("data")
                if not isinstance(data, dict) or data.get("qq") == "all":
                    continue
                user_id = onebot_user_id(data.get("qq"))
                if user_id is not None and user_id not in self.stranger_names:
                    user_ids.add(user_id)
        if user_ids:
            await asyncio.gather(*(self._stranger_name(user_id) for user_id in user_ids))

    async def _media_node(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        url = data.get("url")
        if not isinstance(url, str) or not is_http_url(url):
            return self._quote("[图片无法读取]" if kind == "image" else "[视频无法读取]")
        return {
            "tag": "img" if kind == "image" else "video",
            "attrs": {"src": url},
        }

    @staticmethod
    def _quote(text: str) -> dict[str, Any]:
        return {"tag": "blockquote", "children": [text]}

    @staticmethod
    def _forward_link(page: TelegraphPageRef) -> dict[str, Any]:
        return {
            "tag": "blockquote",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": page.url},
                    "children": [page.title],
                }
            ],
        }

    @staticmethod
    def _title(messages: list[dict[str, Any]]) -> str:
        source = "私聊" if messages and messages[0].get("message_type") == "private" else "群聊"
        return f"{source}的聊天记录 - {token_hex(8)}"


async def create_forward_page(
    forward_id: str,
    gateway: QGateway,
    telegraph: TelegraphClient,
) -> TelegraphPageRef:
    return await ForwardPageBuilder(gateway, telegraph).create(forward_id)
