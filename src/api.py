"""FastAPI 应用层：管理应用生命周期并提供临时媒体 HTTP 接口。"""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse

from src.bus import message_bus
from src.log import baselog
from src.media import MediaStream, media_cache
from src.processing import media_processor
from src.sql import sql
from src.ws import router as ws_router


async def purge_cache() -> None:
    """定期清除过期消息映射和媒体，避免纯惰性清理长期占用资源。"""
    while True:
        await asyncio.sleep(10)
        await sql.purge_expired()
        media_cache.purge_expired()


class MediaResponse(StreamingResponse):
    """在所有 ASGI 退出路径上关闭 MediaStream。

    StreamingResponse 正常完成时会耗尽迭代器，但响应头发送失败、客户端断开或
    任务取消都可能提前退出。finally 保证这些情况仍归还 MediaFile 的读取租约。
    """

    def __init__(
        self,
        stream: MediaStream,
        *,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        # StreamingResponse 会把 body_iterator 标注成通用 AsyncIterable，无法从该
        # 字段看出 aclose。另存具体的 MediaStream，保留准确类型和关闭接口。
        self._media_stream = stream
        super().__init__(stream, media_type=media_type, headers=headers)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._media_stream.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理不依赖 SnowLuma 连接的应用级资源。

    启动顺序：
    1. 打开并迁移数据库；
    2. 启动数据库和媒体缓存的定时清理；
    3. 启动重试调度器和单并发媒体预处理 worker。

    PTB、消息消费者和媒体下载客户端属于 SnowLuma 连接级资源，仅在通过认证的
    WebSocket 连接存续期间运行。
    """
    await sql.load()
    # TTL 清理独立运行，避免没有新请求时过期数据一直留在内存或临时磁盘。
    cache_purger = asyncio.create_task(purge_cache(), name="cache-purger")
    retry_dispatcher = asyncio.create_task(
        message_bus.dispatch_retries(),
        name="retry-dispatcher",
    )
    media_worker = asyncio.create_task(
        media_processor.run(),
        name="media-processing-worker",
    )
    try:
        yield
    finally:
        cache_purger.cancel()
        retry_dispatcher.cancel()
        media_worker.cancel()
        try:
            await cache_purger
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            # 清理任务可能在 lifespan 退出前已经失败，不能因此跳过资源关闭。
            baselog.exception("缓存定时清理任务异常退出")
        finally:
            try:
                await retry_dispatcher
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    await media_worker
                except asyncio.CancelledError:
                    pass
                finally:
                    await media_processor.close()
                    media_cache.close()
                    await sql.close()

# FastAPI 是媒体 HTTP 接口和 SnowLuma WebSocket 路由的统一挂载入口。
fapp = FastAPI(lifespan=lifespan)
fapp.include_router(ws_router)


@fapp.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """供容器运行时检查 ASGI 服务是否仍能响应请求。"""
    return {"status": "ok"}


@fapp.get("/media/{media_id}")
async def get_media(media_id: str) -> Response:
    """向 OneBot 提供 Telegram 媒体的临时下载地址。

    media_id 是不可预测的随机字符串。缓存未命中或 TTL 到期返回 404；命中时
    从 SpooledTemporaryFile 分块读取，不会把整个媒体重新加载成 bytes。
    """
    content = media_cache.get_media(media_id)
    if content is None:
        raise HTTPException(status_code=404, detail="媒体不存在或已过期")
    return MediaResponse(
        content.chunks(),
        media_type=content.media_type,
        headers={
            "Content-Length": str(content.size),
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(content.filename, safe='')}"
            ),
        },
    )
