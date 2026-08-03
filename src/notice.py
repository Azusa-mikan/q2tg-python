from __future__ import annotations

"""把桥接状态提示建模为通用发送任务。"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from src.bus import MessageBus, message_bus
from src.log import baselog
from src.messages import SendLane, SendTarget, SendTask

if TYPE_CHECKING:
    from src.qbot import QGateway


def telegram_notice_task(
    send: Callable[[], Awaitable[Any]],
    *,
    label: str = "telegram-notice",
) -> SendTask:
    """创建单次发送的 Telegram 状态通知任务。"""

    async def send_notice() -> None:
        await send()

    return SendTask(
        target=SendTarget.TELEGRAM,
        lane=SendLane.SYSTEM,
        send=send_notice,
        label=label,
    )


def onebot_notice_task(
    gateway: QGateway,
    *,
    q_group_id: int,
    text: str,
    label: str = "onebot-notice",
) -> SendTask:
    """创建单次发送的 OneBot 状态通知任务。"""

    async def send_notice() -> None:
        await gateway.send_group_message(
            group_id=q_group_id,
            message=[{"type": "text", "data": {"text": text}}],
        )

    return SendTask(
        target=SendTarget.ONEBOT,
        lane=SendLane.SYSTEM,
        send=send_notice,
        label=label,
    )


def enqueue_bridge_notice(
    telegram_send: Callable[[], Awaitable[Any]],
    gateway: QGateway,
    *,
    q_group_id: int | None,
    text: str,
    bus: MessageBus | None = None,
) -> None:
    """分别向 Telegram、OneBot 队列投递通知；任一队列满都不影响另一侧。"""
    bus = bus or message_bus
    _enqueue_notice(
        bus,
        telegram_notice_task(telegram_send, label="bridge-notice:telegram"),
    )
    if q_group_id is not None:
        _enqueue_notice(
            bus,
            onebot_notice_task(
                gateway,
                q_group_id=q_group_id,
                text=text,
                label="bridge-notice:onebot",
            ),
        )


def enqueue_onebot_notice(
    gateway: QGateway,
    *,
    q_group_id: int,
    text: str,
    bus: MessageBus | None = None,
) -> None:
    """向 OneBot 队列投递单侧通知。"""
    bus = bus or message_bus
    _enqueue_notice(
        bus,
        onebot_notice_task(gateway, q_group_id=q_group_id, text=text),
    )


def enqueue_telegram_notice(
    send: Callable[[], Awaitable[Any]],
    *,
    bus: MessageBus | None = None,
) -> None:
    """向 Telegram 队列投递单侧通知。"""
    bus = bus or message_bus
    _enqueue_notice(bus, telegram_notice_task(send))


def _enqueue_notice(bus: MessageBus, task: SendTask) -> None:
    if not bus.put_nowait(task):
        baselog.error("通知目标队列已满，丢弃任务: %s", task.label)
