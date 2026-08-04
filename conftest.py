from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--custom-tests",
        action="store_true",
        help="只运行连接真实 OneBot 与 Telegram 的交互测试",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--custom-tests"):
        config.args[:] = [str(config.rootpath / "custom_tests")]


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    relative_path = collection_path.relative_to(config.rootpath)
    top_level = relative_path.parts[0]
    if config.getoption("--custom-tests"):
        return top_level == "tests"
    return top_level == "custom_tests"
