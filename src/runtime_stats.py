from __future__ import annotations

"""供 Telegram 状态命令读取的进程与队列运行指标。"""

from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

import humanize
import psutil

from src.bus import message_bus
from src.processing import media_processor

CONVERSION_HISTORY_SIZE = 30
ConversionKind = Literal[
    "voice",
    "video",
    "sticker_static",
    "sticker_tgs",
    "sticker_video",
]
# 单一来源的转换种类列表，驱动耗时记录与平均值构建，新增种类只改这里。
CONVERSION_KINDS: tuple[ConversionKind, ...] = (
    "voice",
    "video",
    "sticker_static",
    "sticker_tgs",
    "sticker_video",
)


@dataclass(frozen=True, slots=True)
class QueueSizes:
    onebot_messages: int
    onebot_events: int
    onebot_system: int
    telegram_messages: int
    telegram_events: int
    telegram_system: int
    retry: int
    media_processing: int


@dataclass(frozen=True, slots=True)
class ConversionAverages:
    """各类转换的平均耗时；None 表示尚无成功记录。"""

    voice: float | None
    video: float | None
    sticker_static: float | None
    sticker_tgs: float | None
    sticker_video: float | None


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    rss: str
    queues: QueueSizes
    conversion_averages: ConversionAverages


conversion_times: dict[ConversionKind, deque[float]] = {
    kind: deque(maxlen=CONVERSION_HISTORY_SIZE) for kind in CONVERSION_KINDS
}


def _get_rss() -> str:
    """返回适合状态消息展示的当前进程 RSS。"""
    return humanize.naturalsize(
        (psutil.Process().memory_info().rss),
        binary=True,
        format="%.2f"
    )


def _get_queue_sizes() -> QueueSizes:
    """返回两侧发送、重试及媒体处理队列的当前项目数。"""
    return QueueSizes(
        onebot_messages=message_bus.onebot_queue.qsize(),
        onebot_events=message_bus.onebot_event_queue.qsize(),
        onebot_system=message_bus.onebot_system_queue.qsize(),
        telegram_messages=message_bus.telegram_queue.qsize(),
        telegram_events=message_bus.telegram_event_queue.qsize(),
        telegram_system=message_bus.telegram_system_queue.qsize(),
        retry=message_bus.retry_queue.qsize(),
        media_processing=media_processor.queue.qsize(),
    )


def _average(samples: deque[float]) -> float | None:
    return sum(samples) / len(samples) if samples else None


def _get_conversion_averages() -> ConversionAverages:
    """返回各类转换平均耗时，单位为秒。"""
    return ConversionAverages(
        **{kind: _average(samples) for kind, samples in conversion_times.items()}
    )


def get_runtime_info() -> RuntimeInfo:
    """返回供状态命令使用的完整运行信息快照。"""
    return RuntimeInfo(
        rss=_get_rss(),
        queues=_get_queue_sizes(),
        conversion_averages=_get_conversion_averages(),
    )


async def track_conversion[T](kind: ConversionKind, operation: Awaitable[T]) -> T:
    """执行一次转换，仅在成功后记录耗时。"""
    started_at = perf_counter()
    result = await operation
    conversion_times[kind].append(perf_counter() - started_at)
    return result
