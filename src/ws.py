"""SnowLuma WebSocket 认证、路由及连接级资源生命周期。"""

import asyncio
from secrets import compare_digest
from typing import Annotated

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import JSONResponse

from src.bus import message_bus
from src.config import config
from src.lifecycle import await_cancelled
from src.log import baselog
from src.messages import SendLane, SendTarget
from src.qbot import q_gateway, receive_onebot_event
from src.runtime_events import emit_runtime_event
from src.tgbot import TGBot

# PTB 只在通过认证的 SnowLuma 会话存续期间运行，但重连时复用同一个 Application。
tgbot = TGBot(config.tgbot_token, proxy_url=config.tgbot_proxy_url)
router = APIRouter()

# QGateway 只维护一个活动 WebSocket，因此拒绝并发的第二个 SnowLuma 连接。
connection_lock = asyncio.Lock()


async def verify_snowluma_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """校验 SnowLuma WebSocket 握手中的 Bearer token。"""
    if authorization is None:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="缺少 Authorization Bearer token",
        )

    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or not compare_digest(token, config.onebot_token)
    ):
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Authorization Bearer token 无效",
        )


@router.websocket("/ws", dependencies=[Depends(verify_snowluma_token)])
async def snowluma_ws(websocket: WebSocket) -> None:
    """在 SnowLuma 连接期间运行 OneBot 接收循环和 Telegram 桥接资源。"""
    if connection_lock.locked():
        await websocket.send_denial_response(
            JSONResponse(
                content={"detail": "只能有一个 SnowLuma 实例连接"},
                status_code=403,
            )
        )
        return

    async with connection_lock:
        await websocket.accept()
        # 下载客户端和消费者只服务于当前会话，断线后关闭，重连时重新创建。
        onebot_download_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            proxy=config.onebot_proxy_url,
            trust_env=False,
        )
        telegram_download_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
            proxy=config.tgbot_proxy_url,
            trust_env=False,
        )
        tgbot.download_client = telegram_download_client
        q_gateway.bind(websocket)
        consumers: dict[tuple[SendTarget, SendLane], asyncio.Task[None]] = {}
        try:
            await tgbot.run()
            # 先完成两侧客户端初始化，再处理跨连接保留的发送任务。
            for target in SendTarget:
                for lane in SendLane:
                    consumers[target, lane] = asyncio.create_task(
                        message_bus.consume(target, lane),
                        name=f"{target.name.lower()}-{lane.name.lower()}-consumer",
                    )
            emit_runtime_event("bridge.ready", "onebot-websocket")
            while True:
                data = await websocket.receive_json()
                if q_gateway.resolve_response(data):
                    continue
                await receive_onebot_event(data, tgbot.app.bot, onebot_download_client)
        except WebSocketDisconnect:
            baselog.warning("Snowluma 已断开连接")
        finally:
            q_gateway.unbind(websocket)
            # OneBot worker 与当前连接绑定。取消时，正在执行的通用任务会进入
            # retry_queue；尚未开始的任务继续留在 OneBot 队列等待下一次连接。
            onebot_consumers = [
                task
                for (target, _), task in consumers.items()
                if target is SendTarget.ONEBOT
            ]
            for consumer in onebot_consumers:
                consumer.cancel()
            for consumer in onebot_consumers:
                await await_cancelled(consumer)
            try:
                # 停止 Telegram 生产新消息，但保留 Bot API 客户端供队列排空。
                await tgbot.stop()
            finally:
                # OneBot reader 已停止，不会再产生 TG 任务。先等待其独立重试链路
                # 稳定排空，再放停止信号，避免重试任务落到哨兵后面。
                telegram_consumers = {
                    lane: task
                    for (target, lane), task in consumers.items()
                    if target is SendTarget.TELEGRAM
                }
                for lane in telegram_consumers:
                    await message_bus.join(SendTarget.TELEGRAM, lane)
                for lane in telegram_consumers:
                    await message_bus.stop_consumer(SendTarget.TELEGRAM, lane)
                try:
                    for consumer in telegram_consumers.values():
                        await consumer
                finally:
                    try:
                        await tgbot.shutdown()
                    finally:
                        tgbot.download_client = None
                        try:
                            await telegram_download_client.aclose()
                        finally:
                            await onebot_download_client.aclose()
