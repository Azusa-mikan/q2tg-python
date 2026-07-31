from __future__ import annotations

"""平台无关的内部消息与转发任务模型。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal

from src.media import MediaFile


@dataclass(slots=True, kw_only=True)
class OneBotMessage:
    """经过入口校验的 OneBot 群消息。"""

    message_id: int
    group_id: int
    user_id: int
    sender_name: str
    message: list[dict[Any, Any]]
    sender_name_is_fallback: bool = False
    reply_message_id: int | None = None
    # Telegram 发送可能在多媒体中途失败。进度保存在任务 DTO 中，使重试不会
    # 重复发送已经成功的前序媒体。
    tg_message_ids: list[int] = field(default_factory=list)
    next_media_index: int = 0


@dataclass(slots=True, kw_only=True)
class TelegramMedia:
    """Telegram 入站媒体及其对应的 OneBot 消息段类型。"""

    kind: Literal["file", "image", "video"]
    content: MediaFile
    processing: Literal["none", "video", "sticker_static", "sticker_video"] = "none"


@dataclass(slots=True, kw_only=True)
class TelegramMessage:
    """Telegram 文本、单媒体或已聚合媒体组。"""

    message_ids: tuple[int, ...]
    group_id: int
    user_id: int
    sender_name: str
    text: str | None
    forwarded_from: str | None = None
    reply_message_id: int | None = None
    media: tuple[TelegramMedia, ...] = ()
    media_ids: tuple[str, ...] | None = None
    queue_bytes: int = 0
    q_message_ids: list[int] = field(default_factory=list)
    next_onebot_batch: int = 0


class SendTarget(Enum):
    """发送任务的目标平台。"""

    ONEBOT = auto()
    TELEGRAM = auto()


class FailureAction(Enum):
    """任务失败后由通用总线执行的动作。"""

    DROP = auto()
    RETRY = auto()
    DEFER = auto()


def drop_failure(error: Exception) -> FailureAction:
    """默认策略：记录单次失败，不重试。"""
    return FailureAction.DROP


@dataclass(slots=True, kw_only=True)
class SendTask:
    """平台无关的可发送任务及其失败、耗尽和资源清理策略。"""

    target: SendTarget
    send: Callable[[], Awaitable[None]]
    failure_action: Callable[[Exception], FailureAction] = drop_failure
    max_attempts: int = 1
    failures: int = 0
    on_failed: Callable[[Exception], Awaitable[None]] | None = None
    finalize: Callable[[], Awaitable[None]] | None = None
    label: str = "send-task"


class OneBotSendError(Exception):
    """OneBot 在线时 send_group_msg 返回业务失败。"""


class OneBotConnectionError(Exception):
    """SnowLuma WebSocket 不可用于发送。"""


class MediaTooLargeError(Exception):
    """媒体超过桥接服务允许转发的大小。"""
