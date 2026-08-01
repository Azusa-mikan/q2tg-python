"""临时媒体文件、读取租约以及文件数量/字节数背压工具。"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from secrets import token_urlsafe
from tempfile import SpooledTemporaryFile

from src.paths import ensure_temp_dir

SPOOL_MEMORY_LIMIT = 1 * 1024 * 1024
# 落盘后的 SpooledTemporaryFile 会持续占用一个文件描述符。这里限制的不只是
# Python 对象数量，也是在给系统的 open files 上限留出余量。
MEDIA_RESOURCE_LIMIT = 256
MEDIA_QUEUE_MAX_BYTES = 512 * 1024 * 1024
MEDIA_CACHE_MAX_BYTES = 512 * 1024 * 1024
MEDIA_CACHE_TTL = 120
STREAM_CHUNK_SIZE = 256 * 1024


class ByteBudget:
    """按总字节数提供背压。

    asyncio.Queue(maxsize=100) 只能限制消息条数，但一条消息可能是几字节文本，
    也可能是包含十张图片的相册。生产者在下载图片前先 acquire，消费者处理
    完事后 release，这样排队媒体的总大小达到上限时，生产者会等待而不是
    继续占用内存或临时磁盘。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._condition = asyncio.Condition()

    async def acquire(self, size: int) -> None:
        if size > self.limit:
            raise ValueError(f"资源大小 {size} 超过预算上限 {self.limit}")
        async with self._condition:
            # Condition 会释放锁并休眠；release() 通知后再重新检查条件。
            await self._condition.wait_for(lambda: self.used + size <= self.limit)
            self.used += size

    async def release(self, size: int) -> None:
        async with self._condition:
            self.used -= size
            if self.used < 0:
                raise RuntimeError("媒体队列预算计数错误")
            self._condition.notify_all()


class ItemBudget:
    """限制同时存在的 MediaFile 数量。

    大文件 rollover 到磁盘后，每个 MediaFile 都持有一个打开的临时文件。
    即使总字节数未超限，极多小文件也可能先耗尽系统文件描述符，因此项目
    同时使用“字节预算”和“文件数量预算”。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._waiters: list[asyncio.Future[None]] = []

    async def acquire(self, count: int = 1, *, reserve: int = 0) -> None:
        # reserve 给消费者即时下载留槽，避免队列占满全部槽位后互相等待。
        available = self.limit - reserve
        if count > available:
            raise ValueError(f"资源数量 {count} 超过预算上限 {self.limit}")
        while self.used + count > available:
            # ItemBudget.release() 是同步方法，因此这里使用 Future 作为等待信号，
            # 不要求关闭文件的调用方为了归还一个计数而变成异步函数。
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                self._waiters.remove(waiter)
        self.used += count

    def release(self, count: int = 1) -> None:
        self.used -= count
        if self.used < 0:
            raise RuntimeError("媒体资源数量计数错误")
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)


media_item_budget = ItemBudget(MEDIA_RESOURCE_LIMIT)
media_queue_budget = ByteBudget(MEDIA_QUEUE_MAX_BYTES)


class MediaFile:
    """一个可在内存与临时磁盘之间自动切换的媒体文件。

    文件不超过 SPOOL_MEMORY_LIMIT 时由 SpooledTemporaryFile 保存在内存；超过
    后自动 rollover 到系统临时目录。对象关闭时，底层临时文件会自动删除。

    MediaFile 的所有权必须明确：创建者负责关闭；进入 TelegramMessage 后由任务负责；
    成功加入 MediaCache 后改由媒体缓存负责。读取租约用于保证 HTTP 响应尚未发送完时，
    TTL 清理只把文件标记为待关闭，不会中途截断响应。
    """

    def __init__(
        self,
        file: SpooledTemporaryFile[bytes],
        *,
        filename: str,
        media_type: str,
        owns_item_slot: bool,
    ) -> None:
        self.file = file
        self.filename = filename
        self.media_type = media_type
        self.size = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_pending = False
        self._leases = 0
        self._owns_item_slot = owns_item_slot
        self._close_callbacks: list[Callable[[], None]] = []

    @classmethod
    async def create(
        cls,
        *,
        filename: str = "image",
        media_type: str = "application/octet-stream",
    ) -> "MediaFile":
        # 普通创建路径自行申请一个全局文件名额。
        await media_item_budget.acquire()
        try:
            file = SpooledTemporaryFile(  # noqa: SIM115
                max_size=SPOOL_MEMORY_LIMIT,
                mode="w+b",
                dir=str(ensure_temp_dir()),
            )
        except BaseException:
            media_item_budget.release()
            raise
        return cls(
            file,
            filename=filename,
            media_type=media_type,
            owns_item_slot=True,
        )

    @classmethod
    def create_reserved(
        cls,
        *,
        filename: str = "image",
        media_type: str = "application/octet-stream",
    ) -> "MediaFile":
        # 调用方已批量取得 ItemBudget；这里不能再次 acquire。
        return cls(
            SpooledTemporaryFile(
                max_size=SPOOL_MEMORY_LIMIT,
                mode="w+b",
                dir=str(ensure_temp_dir()),
            ),
            filename=filename,
            media_type=media_type,
            owns_item_slot=True,
        )

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise ValueError("媒体文件已关闭")
        self.file.write(chunk)
        # SpooledTemporaryFile 没有直接暴露业务需要的稳定长度属性，所以写入时
        # 自己累计；下载上限、队列预算和 Content-Length 都使用这个值。
        self.size += len(chunk)

    def rewind(self) -> None:
        if self._closed:
            raise ValueError("媒体文件已关闭")
        self.file.seek(0)

    def chunks(self) -> "MediaStream":
        if self._closed or self._close_pending:
            raise ValueError("媒体文件已关闭")
        # 每个 HTTP 响应取得一个租约。即使还未读取第一个 chunk，响应取消时
        # MediaResponse 也会调用 aclose() 归还它。
        self._leases += 1
        return MediaStream(self)

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        # MediaCache 用回调在文件“真正关闭”时归还字节容量；不能在 URL 过期时提前
        # 归还，因为此时可能仍有一个已经开始的 HTTP 响应持有该文件。
        if self._closed:
            callback()
            return
        self._close_callbacks.append(callback)

    def close(self) -> None:
        # close 可以由多个异常清理路径调用，所以必须保持幂等。
        if self._closed or self._close_pending:
            return
        self._close_pending = True
        # TTL 可以先使 URL 失效，但正在发送的响应仍需读完底层文件。
        if self._leases == 0:
            self._finish_close()

    def _finish_close(self) -> None:
        # 只有没有活跃读取租约时才会进入这里。关闭文件会删除已落盘的临时文件。
        self._closed = True
        self.file.close()
        for callback in self._close_callbacks:
            callback()
        self._close_callbacks.clear()
        if self._owns_item_slot:
            self._owns_item_slot = False
            media_item_budget.release()


class MediaStream(AsyncIterator[bytes]):
    """MediaFile 的单次异步读取会话。

    一个临时 URL 在 TTL 内可以被 SnowLuma 重试，因此同一文件可能被请求多次。
    所有请求共享同一个文件游标，必须用锁串行执行 seek/read，否则两个响应会
    相互移动游标并得到损坏的内容。这里的文件读取本身是同步操作，但每次最多
    STREAM_CHUNK_SIZE，避免一次把整张图片重新读入内存。
    """

    def __init__(self, media: MediaFile) -> None:
        self._media = media
        self._started = False
        self._closed = False

    def __aiter__(self) -> "MediaStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        if not self._started:
            # 锁要持有到整个响应读取结束，而不是每个 chunk 单独加锁，否则两个
            # 响应仍可能在 chunk 之间交错并改变共享游标。
            await self._media._lock.acquire()
            self._media.rewind()
            self._started = True
        chunk = self._media.file.read(STREAM_CHUNK_SIZE)
        if chunk:
            return chunk
        await self.aclose()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        # 正常读完、客户端断开和 ASGI 发送失败最终都会走到这里。
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._media._lock.release()
        self._media._leases -= 1
        if self._media._close_pending and self._media._leases == 0:
            self._media._finish_close()


@dataclass(slots=True)
class CachedMedia:
    """临时媒体文件、URL 过期时间及待发送任务持有的租约数。"""

    content: MediaFile
    expires_at: float
    pins: int = 0


class MediaCache:
    """管理 Telegram 图片的短期 HTTP 下载映射和文件所有权。"""

    def __init__(self) -> None:
        self._media: dict[str, CachedMedia] = {}
        self._media_bytes = 0

    def set_media_batch(
        self,
        contents: Sequence[MediaFile],
        *,
        pinned: bool = False,
    ) -> tuple[str, ...]:
        """整批缓存图片并返回临时 URL ID，成功后接管所有文件。"""
        self.purge_expired()
        batch_size = sum(content.size for content in contents)
        if self._media_bytes + batch_size > MEDIA_CACHE_MAX_BYTES:
            raise ValueError("临时媒体缓存超过 512 MiB 上限")

        expires_at = time.time() + MEDIA_CACHE_TTL
        media_ids = tuple(token_urlsafe(32) for _ in contents)
        for media_id, content in zip(media_ids, contents, strict=True):
            # 活跃 HTTP 响应会延迟关闭文件，容量也必须在真正关闭时才归还。
            content.add_close_callback(lambda size=content.size: self._release_bytes(size))
            self._media[media_id] = CachedMedia(
                content=content,
                expires_at=expires_at,
                pins=int(pinned),
            )
        self._media_bytes += batch_size
        return media_ids

    def get_media(self, media_id: str) -> MediaFile | None:
        """取得未过期媒体；命中过期项时立即使 URL 失效。"""
        media = self._media.get(media_id)
        if media is None:
            return None
        if media.pins == 0 and media.expires_at <= time.time():
            self._remove(media_id, media)
            return None
        return media.content

    def purge_expired(self) -> None:
        """使所有过期 URL 失效，并请求安全关闭对应临时文件。"""
        now = time.time()
        for media_id, media in list(self._media.items()):
            if media.pins == 0 and media.expires_at <= now:
                self._remove(media_id, media)

    def release_media_batch(self, media_ids: Sequence[str]) -> None:
        """释放发送任务租约，并从释放时刻开始计算 URL 的 TTL。"""
        expires_at = time.time() + MEDIA_CACHE_TTL
        for media_id in media_ids:
            media = self._media.get(media_id)
            if media is None:
                continue
            if media.pins <= 0:
                raise RuntimeError("临时媒体缓存租约计数错误")
            media.pins -= 1
            if media.pins == 0:
                media.expires_at = expires_at

    def close(self) -> None:
        """使全部 URL 失效，并请求安全关闭缓存持有的文件。"""
        for media_id, media in list(self._media.items()):
            self._remove(media_id, media)

    def _remove(self, media_id: str, media: CachedMedia) -> None:
        if self._media.pop(media_id, None) is None:
            return
        media.content.close()

    def _release_bytes(self, size: int) -> None:
        # 统计值包含过期但仍被活跃 HTTP 响应读取的文件。
        self._media_bytes -= size


media_cache = MediaCache()
