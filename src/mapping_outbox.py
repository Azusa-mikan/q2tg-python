"""远端发送成功后，持久化等待补写数据库的消息映射。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.lifecycle import await_completion_on_cancel
from src.log import baselog
from src.paths import DATA_DIR
from src.sql import MESSAGE_RETENTION, MessageMapping, Sql, sql

OUTBOX_VERSION = 1
OUTBOX_RETRY_INTERVAL = 10
OUTBOX_PATH = DATA_DIR / "pending-message-mappings.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingMessageMapping:
    q_group_id: int
    q_message_ids: tuple[int, ...]
    tg_chat_id: int
    tg_message_ids: tuple[int, ...]
    q_user_id: int | None = None
    tg_user_id: int | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        payload = json.dumps(
            asdict(self),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        default_created_at: float = 0.0,
    ) -> PendingMessageMapping:
        if not isinstance(payload, dict):
            raise TypeError("消息映射补偿记录不是对象")
        q_group_id = _required_int(payload, "q_group_id")
        tg_chat_id = _required_int(payload, "tg_chat_id")
        q_message_ids = _required_int_tuple(payload, "q_message_ids")
        tg_message_ids = _required_int_tuple(payload, "tg_message_ids")
        q_user_id = _optional_int(payload, "q_user_id")
        tg_user_id = _optional_int(payload, "tg_user_id")
        created_at = _optional_number(payload, "created_at")
        return cls(
            q_group_id=q_group_id,
            q_message_ids=q_message_ids,
            tg_chat_id=tg_chat_id,
            tg_message_ids=tg_message_ids,
            q_user_id=q_user_id,
            tg_user_id=tg_user_id,
            created_at=(default_created_at if created_at is None else created_at),
        )


class MappingOutbox:
    """原子保存待补偿映射，并在数据库恢复后顺序补写。"""

    def __init__(
        self,
        database: Sql = sql,
        *,
        path: Path = OUTBOX_PATH,
        retry_interval: float = OUTBOX_RETRY_INTERVAL,
    ) -> None:
        self._database = database
        self._path = path
        self._retry_interval = retry_interval
        self._pending: dict[str, PendingMessageMapping] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            raise RuntimeError("消息映射补偿队列已经加载")
        records = await await_completion_on_cancel(
            asyncio.to_thread(self._read_records)
        )
        pending: dict[str, PendingMessageMapping] = {}
        for record in records:
            pending = self._with_mapping(pending, record)
        self._pending = pending
        self._loaded = True
        if self._pending:
            self._wake.set()

    async def close(self) -> None:
        async with self._lock:
            self._pending.clear()
            self._wake.clear()
            self._loaded = False

    async def enqueue(self, mapping: PendingMessageMapping) -> None:
        if not self._loaded:
            raise RuntimeError("消息映射补偿队列尚未加载")
        await await_completion_on_cancel(self._enqueue(mapping))

    async def _enqueue(self, mapping: PendingMessageMapping) -> None:
        async with self._lock:
            pending = self._with_mapping(self._pending, mapping)
            await self._persist(pending)
            self._pending = pending
            self._wake.set()

    async def complete(self, mapping: PendingMessageMapping) -> None:
        """确认映射已写入数据库，并从持久补偿文件中移除。"""
        if not self._loaded:
            raise RuntimeError("消息映射补偿队列尚未加载")
        await await_completion_on_cancel(self._complete(mapping))

    async def _complete(self, mapping: PendingMessageMapping) -> None:
        async with self._lock:
            if mapping.key not in self._pending:
                return
            pending = dict(self._pending)
            pending.pop(mapping.key)
            await self._persist(pending)
            self._pending = pending
            if not pending:
                self._wake.clear()

    async def run(self) -> None:
        if not self._loaded:
            raise RuntimeError("消息映射补偿队列尚未加载")
        while True:
            await self._wake.wait()
            async with self._lock:
                mapping = next(iter(self._pending.values()), None)
                if mapping is None:
                    self._wake.clear()
                    continue
            try:
                await self._database.set_message_mapping(
                    **database_values(mapping),
                    replace_older_than=mapping.created_at,
                )
            except Exception:
                baselog.exception("待补偿消息映射写入数据库失败")
                await asyncio.sleep(self._retry_interval)
                continue
            try:
                await self.complete(mapping)
            except Exception:
                baselog.exception("待补偿消息映射确认失败")
                await asyncio.sleep(self._retry_interval)

    def get_tg_message(
        self,
        q_group_id: int,
        q_message_id: int,
    ) -> PendingMessageMapping | None:
        """查询尚未写入数据库的 OneBot 来源映射。"""
        return next(
            (
                mapping
                for mapping in reversed(self._pending.values())
                if mapping.q_group_id == q_group_id
                and q_message_id in mapping.q_message_ids
            ),
            None,
        )

    def get_q_message(
        self,
        tg_chat_id: int,
        tg_message_id: int,
    ) -> PendingMessageMapping | None:
        """查询尚未写入数据库的 Telegram 来源映射。"""
        return next(
            (
                mapping
                for mapping in reversed(self._pending.values())
                if mapping.tg_chat_id == tg_chat_id
                and tg_message_id in mapping.tg_message_ids
            ),
            None,
        )

    def pending_count(self) -> int:
        return len(self._pending)

    async def _persist(
        self,
        pending: dict[str, PendingMessageMapping],
    ) -> None:
        payload = {
            "version": OUTBOX_VERSION,
            "mappings": [asdict(mapping) for mapping in pending.values()],
        }
        await await_completion_on_cancel(
            asyncio.to_thread(self._write_payload, payload)
        )

    @staticmethod
    def _with_mapping(
        pending: dict[str, PendingMessageMapping],
        mapping: PendingMessageMapping,
    ) -> dict[str, PendingMessageMapping]:
        updated = {
            key: existing
            for key, existing in pending.items()
            if not (
                (
                    existing.q_group_id == mapping.q_group_id
                    and set(existing.q_message_ids) & set(mapping.q_message_ids)
                )
                or (
                    existing.tg_chat_id == mapping.tg_chat_id
                    and set(existing.tg_message_ids) & set(mapping.tg_message_ids)
                )
            )
        }
        updated[mapping.key] = mapping
        return updated

    def _read_records(self) -> list[PendingMessageMapping]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("消息映射补偿文件无法读取") from error
        if not isinstance(payload, dict) or payload.get("version") != OUTBOX_VERSION:
            raise RuntimeError("消息映射补偿文件版本无法识别")
        mappings = payload.get("mappings")
        if not isinstance(mappings, list):
            raise TypeError("消息映射补偿文件缺少 mappings 数组")
        modified_at = self._path.stat().st_mtime
        return [
            PendingMessageMapping.from_payload(
                mapping,
                default_created_at=modified_at,
            )
            for mapping in mappings
        ]

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(
                    payload,
                    output,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self._path)
            directory_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"消息映射补偿记录的 {key} 不是整数")
    return value


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return _required_int(payload, key)


def _optional_number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise TypeError(f"消息映射补偿记录的 {key} 不是非负数")
    return float(value)


def _required_int_tuple(payload: dict[str, Any], key: str) -> tuple[int, ...]:
    value = payload.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
        or len(set(value)) != len(value)
    ):
        raise RuntimeError(f"消息映射补偿记录的 {key} 不是非空唯一整数数组")
    return tuple(value)


def database_values(mapping: PendingMessageMapping) -> dict[str, Any]:
    """返回 Sql.set_message_mapping 接受的映射字段。"""
    values = asdict(mapping)
    values.pop("created_at")
    return values


def newest_mapping(
    database_mapping: MessageMapping | None,
    pending_mapping: PendingMessageMapping | None,
) -> MessageMapping | PendingMessageMapping | None:
    """按创建时间选择数据库或补偿队列中的较新映射。"""
    if pending_mapping is None:
        return database_mapping
    if (
        database_mapping is None
        or pending_mapping.created_at + MESSAGE_RETENTION > database_mapping.expires_at
    ):
        return pending_mapping
    return database_mapping


def newest_pending_mapping(
    first: PendingMessageMapping | None,
    second: PendingMessageMapping | None,
) -> PendingMessageMapping | None:
    """返回两次无锁读取中较新的补偿映射。"""
    if first is None:
        return second
    if second is None or first.created_at >= second.created_at:
        return first
    return second


mapping_outbox = MappingOutbox()
