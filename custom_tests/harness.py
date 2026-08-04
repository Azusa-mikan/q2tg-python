"""真实交互测试的数据、状态和服务生命周期。"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import TracebackType
from typing import Self, TextIO

import uvicorn

from src.paths import ensure_temp_dir
from src.runtime_events import RuntimeEvent, RuntimeObserver, install_runtime_observer

ITEM_TIMEOUT = 60.0
RECENT_RUN_WINDOW = timedelta(minutes=5)
STATE_PATH = ensure_temp_dir() / "custom-tests-state.json"
LOCK_PATH = ensure_temp_dir() / "custom-tests.lock"


def terminal_print(message: str) -> None:
    with open("/dev/tty", "w", encoding="utf-8") as terminal:
        print(message, file=terminal, flush=True)


@dataclass(frozen=True, slots=True)
class TestItem:
    __test__ = False

    key: str
    prompt: str
    phase: str = "send.succeeded"
    label_prefix: str = ""
    error_type: str | None = None
    capability: str | None = None


TEST_ITEMS: tuple[TestItem, ...] = (
    TestItem("text-q-to-tg", "在 OneBot 群发送一条普通文字消息。", label_prefix="onebot-to-telegram:"),
    TestItem("text-tg-to-q", "在 Telegram 群发送一条普通文字消息。", label_prefix="telegram-to-onebot:"),
    TestItem("reply-q-to-tg", "在 OneBot 群回复一条已有映射的消息。", label_prefix="onebot-to-telegram:", capability="onebot.reply.mapped"),
    TestItem("reply-tg-to-q", "在 Telegram 群回复一条已有映射的消息。", label_prefix="telegram-to-onebot:", capability="telegram.reply.mapped"),
    TestItem("reply-missing-q-to-tg", "在 OneBot 群回复一条没有跨平台映射的旧消息；应显示无法读取占位。（通常是bot未启动前发的消息）", label_prefix="onebot-to-telegram:", capability="onebot.reply.unavailable"),
    TestItem("reply-missing-tg-to-q", "在 Telegram 群回复一条没有跨平台映射的旧消息；应显示无法读取占位。（通常是bot未启动前发的消息）", label_prefix="telegram-to-onebot:", capability="telegram.reply.unavailable"),
    TestItem("image-caption-q-to-tg", "在 OneBot 群发送一张带附加文字的图片（不是图片有文字）。", label_prefix="onebot-to-telegram:", capability="onebot.image.photo"),
    TestItem("image-caption-tg-to-q", "在 Telegram 群发送一张带附加说明的图片。", label_prefix="telegram-to-onebot:", capability="telegram.media.image"),
    TestItem("image-album-q-to-tg", "在 OneBot 群一次发送 2 至 10 张图片，每张不超过 10 MB且合计不超过 20 MB。", label_prefix="onebot-to-telegram:", capability="onebot.image-album.photo"),
    TestItem("image-album-document-q-to-tg", "在 OneBot 群一次发送 2 张图片，其中一张必须大于 20 MB 但不能触发客户端自带的文件发送。", label_prefix="onebot-to-telegram:", capability="onebot.image-album.document"),
    TestItem("image-document-q-to-tg", "在 OneBot 群发送一张大于 10 MB、不超过 20 MB 的图片。", label_prefix="onebot-to-telegram:", capability="onebot.image.document"),
    TestItem("image-too-large-q-to-tg", "在 OneBot 群发送一张大于 20 MB 的图片；应收到不转发提示。", label_prefix="onebot-media-rejected:", capability="onebot.media.rejected"),
    TestItem("video-q-to-tg", "在 OneBot 群发送一个不超过 20 MB 的视频。", label_prefix="onebot-to-telegram:", capability="onebot.media.video"),
    TestItem("video-compatible-tg-to-q", "在 Telegram 群发送一个 H.264/AAC、不超过 20 MB 的视频。", label_prefix="telegram-to-onebot:", capability="telegram.video.compatible"),
    TestItem("video-transcode-tg-to-q", "使用 Telegram Desktop 将一个非 H.264 或非 AAC、不超过 20 MB 的素材作为视频发送。（容器格式需mp4，其它格式会被以文件发送）", label_prefix="telegram-to-onebot:", capability="telegram.video.transcoded"),
    TestItem("video-too-large-q-to-tg", "在 OneBot 群发送一个大于 20 MB 的视频；应收到不转发提示。", label_prefix="onebot-media-rejected:", capability="onebot.media.rejected"),
    TestItem("video-too-large-tg-to-q", "在 Telegram 群发送一个大于 20 MB 的视频；应收到不转发提示。", phase="inbound.rejected", label_prefix="telegram-media:video", error_type="ValueError"),
    TestItem("file-q-to-tg", "在 OneBot 群发送一个不超过 20 MB 的文件。", label_prefix="onebot-to-telegram:", capability="onebot.media.file"),
    TestItem("file-tg-to-q", "在 Telegram 群发送一个不超过 20 MB 的文件。", label_prefix="telegram-to-onebot:", capability="telegram.media.file"),
    TestItem("file-too-large-q-to-tg", "在 OneBot 群发送一个大于 20 MB 的文件；应收到不转发提示。", label_prefix="onebot-media-rejected:", capability="onebot.media.rejected"),
    TestItem("file-too-large-tg-to-q", "在 Telegram 群发送一个大于 20 MB 的文件；应收到不转发提示。", phase="inbound.rejected", label_prefix="telegram-media:file", error_type="ValueError"),
    TestItem("voice-normalize-q-to-tg", "在 OneBot 群发送一条普通 QQ 语音；Telegram 应收到 Ogg/Opus 语音。", label_prefix="onebot-to-telegram:", capability="onebot.voice.transcoded"),
    TestItem("voice-tg-to-q", "在 Telegram 群发送一条普通语音。", label_prefix="telegram-to-onebot:", capability="telegram.media.record"),
    TestItem("sticker-static-tg-to-q", "在 Telegram 群发送一个静态贴纸。", label_prefix="telegram-to-onebot:", capability="telegram.sticker.static"),
    TestItem("sticker-video-tg-to-q", "在 Telegram 群发送一个视频贴纸。", label_prefix="telegram-to-onebot:", capability="telegram.sticker.video"),
    TestItem("sticker-tgs-tg-to-q", "在 Telegram 群发送一个 TGS 动态贴纸。", label_prefix="telegram-to-onebot:", capability="telegram.sticker.tgs"),
    TestItem("gif-q-to-tg", "在 OneBot 群以图片段发送一个 GIF；Telegram 应收到动画。", label_prefix="onebot-to-telegram:", capability="onebot.image.animation"),
    TestItem("media-group-tg-to-q", "在 Telegram 群发送一个恰好 10 项、合计不超过 20 MB 的媒体组。", label_prefix="telegram-to-onebot:", capability="telegram.media-group.limit"),
    TestItem("recall-q-to-tg", "先从 OneBot 群发送一条消息，转发后在 OneBot 侧正常撤回。", label_prefix="onebot-recall:"),
    TestItem("undo-tg", "在 Telegram 群回复一条已有映射的消息并使用 /undo。", phase="command.succeeded", label_prefix="telegram-undo"),
    TestItem("member-join-q-to-tg", "触发 OneBot 群成员加入事件。", label_prefix="onebot-group-increase:"),
    TestItem("member-leave-q-to-tg", "触发 OneBot 群成员退出事件。", label_prefix="onebot-group-decrease:"),
    TestItem("member-join-tg-to-q", "触发 Telegram 群成员加入事件。", label_prefix="telegram-group-increase:"),
    TestItem("member-leave-tg-to-q", "触发 Telegram 群成员退出事件。", label_prefix="telegram-group-decrease:"),
    TestItem("qface-q-to-tg", "在 OneBot 群发送一个普通小表情（QFace）。", label_prefix="onebot-to-telegram:", capability="onebot.face.qface"),
    TestItem("super-face-q-to-tg", "在 OneBot 群单独发送一个超级表情。", label_prefix="onebot-to-telegram:", capability="onebot.face.super"),
    TestItem("essence-add-q-to-tg", "将一条 OneBot 消息添加为精华；Telegram 应同步置顶。", label_prefix="onebot-essence-add:"),
    TestItem("essence-delete-q-to-tg", "删除刚才的 OneBot 精华；Telegram 应同步取消置顶。", label_prefix="onebot-essence-delete:"),
    TestItem("pin-tg-to-q", "在 Telegram 置顶一条已有映射的消息。", label_prefix="telegram-pin:"),
    TestItem("unpin-tg-to-q", "在 Telegram 回复已置顶消息并使用 /unpin。", phase="command.succeeded", label_prefix="telegram-unpin"),
    TestItem("ban-q-to-tg", "在 OneBot 群禁言一名成员。", label_prefix="onebot-group-ban:"),
    TestItem("unban-q-to-tg", "在 OneBot 群解除该成员禁言。", label_prefix="onebot-group-ban:"),
    TestItem("poke-q-to-tg", "在 OneBot 群触发一次戳一戳。", label_prefix="onebot-poke:"),
    TestItem("announcement-q-to-tg", "在 OneBot 群发布一条群公告。", label_prefix="onebot-to-telegram:", capability="onebot.announcement"),
    TestItem("forward-q-to-tg", "在 OneBot 群发送一条合并转发，确保 Q2TG 可访问 Telegraph。", label_prefix="onebot-to-telegram:", capability="onebot.forward.telegraph"),
)


class EventCollector:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[RuntimeEvent] = []
        self._closed = False

    def __call__(self, event: RuntimeEvent) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    def position(self) -> int:
        with self._condition:
            return len(self._events)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def wait_for(self, item: TestItem, start: int, timeout: float) -> RuntimeEvent | None:
        deadline = monotonic() + timeout
        capability_work_ids: set[str] = set()
        observed_capabilities: dict[str, set[str]] = {}
        with self._condition:
            while True:
                for event in self._events[start:]:
                    if (
                        event.phase == "send.failed"
                        and event.label.startswith(item.label_prefix)
                    ):
                        error_type = event.error_type or "未知错误"
                        raise AssertionError(
                            f"发送任务 {event.label} 失败：{error_type}"
                        )
                    if (
                        event.phase == "capability.succeeded"
                        and event.work_id is not None
                    ):
                        observed_capabilities.setdefault(event.work_id, set()).add(event.label)
                    if (
                        event.phase == "capability.succeeded"
                        and event.label == item.capability
                        and event.work_id is not None
                    ):
                        capability_work_ids.add(event.work_id)
                    if (
                        event.phase == "capability.invalidated"
                        and event.label == item.capability
                        and event.work_id is not None
                    ):
                        capability_work_ids.discard(event.work_id)
                    capability_seen = item.capability is None or any(
                        _same_work(work_id, event.label) for work_id in capability_work_ids
                    )
                    if (
                        capability_seen
                        and event.phase == item.phase
                        and event.label.startswith(item.label_prefix)
                        and (item.error_type is None or event.error_type == item.error_type)
                    ):
                        return event
                    if (
                        item.capability is not None
                        and event.phase == item.phase
                        and event.label.startswith(item.label_prefix)
                    ):
                        actual = sorted(
                            capability
                            for work_id, capabilities in observed_capabilities.items()
                            if _same_work(work_id, event.label)
                            for capability in capabilities
                        )
                        if actual and item.capability not in actual:
                            raise AssertionError(
                                f"预期能力分支 {item.capability}，实际观察到 {', '.join(actual)}"
                            )
                if self._closed:
                    return None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)


class CustomTestSession:
    def __init__(self) -> None:
        self.collector = EventCollector()
        self.completed: list[str] = []
        self._lock: TextIO | None = None
        self._previous_observer: RuntimeObserver | None = None
        self._observer_installed = False
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._server_error: BaseException | None = None

    def __enter__(self) -> Self:
        try:
            terminal_print("\n正在启动真实 OneBot/Telegram 交互测试。")
            self._lock = _acquire_lock()
            state = _load_state()
            self.completed = _completed_keys(state)
            _reject_recent_completed_run(state, self.completed)
            if len(self.completed) == len(TEST_ITEMS):
                self.completed = []
                _save_state(self.completed)

            self._previous_observer = install_runtime_observer(self.collector)
            terminal_print(
                f"已读取测试进度：{len(self.completed)}/{len(TEST_ITEMS)} 项完成。"
            )
            terminal_print("正在启动 Uvicorn，并等待 OneBot WebSocket 连接（最多一分钟）……")
            self._observer_installed = True
            self._start_server()
            ready = TestItem(
                "bridge-ready",
                "",
                phase="bridge.ready",
                label_prefix="onebot-websocket",
            )
            if self.collector.wait_for(ready, 0, ITEM_TIMEOUT) is None:
                self._raise_server_error()
                raise RuntimeError("OneBot WebSocket 未在一分钟内连接")
            terminal_print(
                f"\nOneBot WebSocket 已连接，桥接就绪。共有 {len(TEST_ITEMS)} 项，"
                f"已完成 {len(self.completed)} 项。"
                "测试期间请只执行当前提示的操作。"
            )
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def run(self, item: TestItem) -> None:
        if item.key in self.completed:
            return
        index = TEST_ITEMS.index(item) + 1
        start = self.collector.position()
        terminal_print(f"\n[{index}/{len(TEST_ITEMS)}] {item.prompt}")
        event = self.collector.wait_for(item, start, ITEM_TIMEOUT)
        if event is None:
            _save_state(self.completed)
            self._raise_server_error()
            if self._server_thread is not None and not self._server_thread.is_alive():
                raise RuntimeError("Uvicorn 在交互测试完成前停止")
            raise AssertionError(f"{item.key} 一分钟内未观察到预期运行事件")

        self.completed.append(item.key)
        completed_at = datetime.now(UTC) if len(self.completed) == len(TEST_ITEMS) else None
        _save_state(self.completed, completed_at=completed_at)
        terminal_print(f"已完成: {item.key}")

    def close(self) -> None:
        shutdown_error: RuntimeError | None = None
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(ITEM_TIMEOUT)
            if self._server_thread.is_alive():
                shutdown_error = RuntimeError("Uvicorn 未在一分钟内停止")
        self.collector.close()
        if self._observer_installed:
            install_runtime_observer(self._previous_observer)
            self._observer_installed = False
        if self._lock is not None:
            self._lock.close()
        self._server = None
        self._server_thread = None
        self._lock = None
        if shutdown_error is not None:
            raise shutdown_error

    def _start_server(self) -> None:
        from src.api import fapp
        from src.config import config

        self._server = uvicorn.Server(
            uvicorn.Config(
                fapp,
                host="0.0.0.0",
                port=config.app_port,
                log_config=None,
            )
        )

        def serve() -> None:
            try:
                assert self._server is not None
                self._server.run()
            except Exception as error:
                self._server_error = error
            finally:
                self.collector.close()

        self._server_thread = threading.Thread(
            target=serve,
            name="custom-tests-uvicorn",
            daemon=False,
        )
        self._server_thread.start()

    def _raise_server_error(self) -> None:
        if self._server_error is not None:
            raise RuntimeError("Uvicorn 运行失败") from self._server_error


def _same_work(capability_work_id: str, terminal_label: str) -> bool:
    if capability_work_id == terminal_label:
        return True
    return capability_work_id.partition(":")[2] == terminal_label.partition(":")[2]


def _load_state() -> dict[str, object]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"真实测试状态文件无法读取: {STATE_PATH}") from error
    if not isinstance(data, dict):
        raise TypeError(f"真实测试状态文件格式错误: {STATE_PATH}")
    return data


def _save_state(completed: list[str], *, completed_at: datetime | None = None) -> None:
    state: dict[str, object] = {
        "completed": completed,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if completed_at is not None:
        state["completed_at"] = completed_at.isoformat()
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def _completed_keys(state: dict[str, object]) -> list[str]:
    completed = state.get("completed")
    if completed is None:
        return []
    if not isinstance(completed, list) or not all(isinstance(key, str) for key in completed):
        raise RuntimeError("真实测试状态中的 completed 字段格式错误")
    known = {item.key for item in TEST_ITEMS}
    return [key for key in completed if key in known]


def _reject_recent_completed_run(state: dict[str, object], completed: list[str]) -> None:
    if len(completed) != len(TEST_ITEMS):
        return
    value = state.get("completed_at")
    if not isinstance(value, str):
        return
    try:
        completed_at = datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError("真实测试状态中的完成时间格式错误") from error
    if completed_at.tzinfo is None:
        raise RuntimeError("真实测试状态中的完成时间缺少时区")
    remaining = RECENT_RUN_WINDOW - (datetime.now(UTC) - completed_at.astimezone(UTC))
    if remaining > timedelta(0):
        raise RuntimeError(f"完整真实测试刚刚运行过，{remaining.seconds + 1} 秒后才能再次运行")


def _acquire_lock() -> TextIO:
    lock = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("另一个真实测试进程正在运行") from None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock
