from collections.abc import Iterator

import pytest

from custom_tests.harness import TEST_ITEMS, CustomTestSession, TestItem


@pytest.fixture(scope="session")
def custom_test_session() -> Iterator[CustomTestSession]:
    with CustomTestSession() as session:
        yield session


@pytest.mark.parametrize("item", TEST_ITEMS, ids=lambda item: item.key)
def test_real_onebot_telegram_interaction(
    request: pytest.FixtureRequest,
    custom_test_session: CustomTestSession,
    item: TestItem,
) -> None:
    if item.key in custom_test_session.completed:
        pytest.skip("已在先前运行中完成")
    try:
        custom_test_session.run(item)
    except Exception:
        request.session.shouldstop = f"真实交互测试在 {item.key} 失败，停止后续项目"
        raise
