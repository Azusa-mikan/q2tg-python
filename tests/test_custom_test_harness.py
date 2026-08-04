import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import custom_tests.test_interactions as interactions
from custom_tests.harness import (
    CustomTestSession,
    EventCollector,
    TestItem,
    _completed_keys,
    _load_state,
    _save_state,
    terminal_print,
)
from src.runtime_events import RuntimeEvent


def test_terminal_print_writes_directly_to_controlling_terminal() -> None:
    terminal = Mock()
    terminal.__enter__ = Mock(return_value=terminal)
    terminal.__exit__ = Mock(return_value=False)

    with patch("custom_tests.harness.open", return_value=terminal) as open_terminal:
        terminal_print("虚构交互提示")

    open_terminal.assert_called_once_with("/dev/tty", "w", encoding="utf-8")
    terminal.write.assert_any_call("虚构交互提示")
    terminal.flush.assert_called_once()


def test_event_collector_matches_capability_and_terminal_work() -> None:
    collector = EventCollector()
    item = TestItem(
        "example",
        "执行虚构操作",
        label_prefix="onebot-to-telegram:",
        capability="onebot.image.photo",
    )
    collector(
        RuntimeEvent(
            phase="capability.succeeded",
            label="onebot.image.photo",
            work_id="onebot-to-telegram:example-work",
        )
    )
    terminal = RuntimeEvent(
        phase="send.succeeded",
        label="onebot-to-telegram:example-work",
    )
    collector(terminal)

    assert collector.wait_for(item, 0, 0.01) is terminal


def test_event_collector_reports_wrong_capability_branch_immediately() -> None:
    collector = EventCollector()
    item = TestItem(
        "example",
        "执行虚构操作",
        label_prefix="onebot-to-telegram:",
        capability="onebot.image-album.document",
    )
    collector(
        RuntimeEvent(
            phase="capability.succeeded",
            label="onebot.image-album.photo",
            work_id="onebot-to-telegram:example-work",
        )
    )
    collector(
        RuntimeEvent(
            phase="send.succeeded",
            label="onebot-to-telegram:example-work",
        )
    )

    with pytest.raises(
        AssertionError,
        match=(
            "预期能力分支 onebot.image-album.document，"
            "实际观察到 onebot.image-album.photo"
        ),
    ):
        collector.wait_for(item, 0, 0.01)


def test_event_collector_reports_terminal_send_failure_immediately() -> None:
    collector = EventCollector()
    item = TestItem(
        "example",
        "执行虚构操作",
        label_prefix="onebot-to-telegram:",
    )
    collector(
        RuntimeEvent(
            phase="send.failed",
            label="onebot-to-telegram:example-work",
            error_type="NetworkError",
        )
    )

    with pytest.raises(
        AssertionError,
        match="发送任务 onebot-to-telegram:example-work 失败：NetworkError",
    ):
        collector.wait_for(item, 0, 0.01)


def test_state_round_trip_filters_unknown_items(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    with patch("custom_tests.harness.STATE_PATH", state_path):
        _save_state(["text-q-to-tg", "unknown-item"])
        state = _load_state()

    assert "suite_id" not in state
    assert _completed_keys(state) == ["text-q-to-tg"]


def test_state_ignores_legacy_suite_id() -> None:
    state = {
        "suite_id": "outdated-suite-id",
        "completed": ["text-q-to-tg", "unknown-item"],
    }

    assert _completed_keys(state) == ["text-q-to-tg"]


def test_session_run_saves_completed_item() -> None:
    session = CustomTestSession()
    item = TestItem("example", "执行虚构操作")
    event = RuntimeEvent(phase="send.succeeded", label="example")
    session.collector.wait_for = Mock(return_value=event)  # type: ignore[method-assign]

    with (
        patch("custom_tests.harness.TEST_ITEMS", (item,)),
        patch("custom_tests.harness._save_state") as save_state,
        patch("custom_tests.harness.terminal_print"),
    ):
        session.run(item)

    assert session.completed == ["example"]
    save_state.assert_called_once()
    assert save_state.call_args.kwargs["completed_at"] is not None


def test_session_close_releases_resources_when_server_does_not_stop() -> None:
    session = CustomTestSession()
    server = SimpleNamespace(should_exit=False)
    server_thread = Mock(spec=threading.Thread)
    server_thread.is_alive.return_value = True
    lock = Mock()
    previous_observer = Mock()
    session._server = server  # type: ignore[assignment]
    session._server_thread = server_thread
    session._lock = lock
    session._previous_observer = previous_observer
    session._observer_installed = True

    with (
        patch("custom_tests.harness.install_runtime_observer") as install_observer,
        pytest.raises(RuntimeError, match="Uvicorn 未在一分钟内停止"),
    ):
        session.close()

    assert server.should_exit
    lock.close.assert_called_once()
    install_observer.assert_called_once_with(previous_observer)


def test_interaction_failure_stops_remaining_pytest_items() -> None:
    item = TestItem("example", "执行虚构操作")
    pytest_session = SimpleNamespace(shouldstop=False)
    request = SimpleNamespace(session=pytest_session)
    custom_session = SimpleNamespace(completed=[], run=Mock(side_effect=AssertionError("超时")))

    with pytest.raises(AssertionError, match="超时"):
        interactions.test_real_onebot_telegram_interaction(
            request,  # type: ignore[arg-type]
            custom_session,  # type: ignore[arg-type]
            item,
        )

    assert pytest_session.shouldstop == "真实交互测试在 example 失败，停止后续项目"
