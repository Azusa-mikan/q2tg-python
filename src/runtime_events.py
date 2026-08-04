"""供真实联调观察运行状态的可选事件接口。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from src.log import baselog


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvent:
    """不包含消息正文、名称、URL 或凭据的运行事件。"""

    phase: str
    label: str
    error_type: str | None = None
    work_id: str | None = None
    timestamp: float = 0.0


RuntimeObserver = Callable[[RuntimeEvent], None]
_observer: RuntimeObserver | None = None
_observer_lock = Lock()
_work_id: ContextVar[str | None] = ContextVar("runtime_work_id", default=None)


@contextmanager
def runtime_work(work_id: str):
    """把当前同步或异步调用链产生的能力事件关联到一项工作。"""
    token = _work_id.set(work_id)
    try:
        yield
    finally:
        _work_id.reset(token)


def install_runtime_observer(observer: RuntimeObserver | None) -> RuntimeObserver | None:
    """安装观察器并返回旧值；正常服务不安装时仅有一次空值判断。"""
    global _observer
    with _observer_lock:
        previous = _observer
        _observer = observer
    return previous


def emit_runtime_event(
    phase: str,
    label: str,
    *,
    error: BaseException | None = None,
    work_id: str | None = None,
) -> None:
    """同步发送脱敏事件；观察器故障不得影响生产逻辑。"""
    with _observer_lock:
        observer = _observer
    if observer is None:
        return
    event = RuntimeEvent(
        phase=phase,
        label=label,
        error_type=type(error).__name__ if error is not None else None,
        work_id=work_id if work_id is not None else _work_id.get(),
        timestamp=monotonic(),
    )
    try:
        observer(event)
    except Exception:  # noqa: BLE001
        baselog.exception("运行时观察器处理事件失败: phase=%s label=%s", phase, label)
