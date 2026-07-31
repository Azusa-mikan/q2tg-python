"""q2tg 服务的命令行启动入口。"""

import uvicorn

from src.api import fapp
from src.config import config


def main() -> None:
    """启动 FastAPI 应用。

    SQLite 和媒体预处理 worker 由 FastAPI lifespan 管理；Telegram Bot、消息消费者和媒体下载客户端
    只在通过认证的 SnowLuma WebSocket 连接存续期间运行。
    """
    uvicorn.run(
        fapp,
        host="0.0.0.0",
        port=config.app_port,
        log_config=None,
    )


if __name__ == "__main__":
    # 使用 ``uv run python main.py`` 时，从这里进入应用。
    main()
