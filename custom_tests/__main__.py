"""真实 OneBot/Telegram 交互式测试的兼容入口。"""

import sys

import pytest


def main() -> None:
    raise SystemExit(pytest.main(["--custom-tests", "-s", *sys.argv[1:]]))


if __name__ == "__main__":
    main()
