"""项目的全局日志格式、命名和第三方日志降噪设置。"""

import logging


class _RenameToFastAPI(logging.Filter):
    """把 Uvicorn 的多个 logger 名称统一显示成 fastapi。"""

    def filter(self, record):
        # Filter 返回 True 表示保留该条日志；这里只修改展示名称，不过滤内容。
        record.name = "fastapi"
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 两个平台分别使用独立 logger，便于按来源筛选日志。
qlog = logging.getLogger("QBot")
baselog = logging.getLogger("q2tg")

# HTTPX 请求可能包含 Telegram Bot Token 或临时媒体 URL，禁止输出 debug 请求日志。
logging.getLogger("httpx").setLevel(logging.WARNING)

# Uvicorn 会使用多个 logger；统一加过滤器后，终端输出更容易阅读。
for _uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_uvicorn_logger).addFilter(_RenameToFastAPI())
