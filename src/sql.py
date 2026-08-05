"""使用 SQLAlchemy Async 保存群绑定和长期消息映射。"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from src.config import config
from src.database_schema import (
    application_settings,
    group_mappings,
    message_mappings,
    onebot_message_mappings,
    telegram_message_mappings,
)
from src.lifecycle import await_completion_on_cancel
from src.sql_migrations import migrate_database

MESSAGE_RETENTION = 30 * 24 * 60 * 60

ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "mysql": "mysql+asyncmy",
    "postgresql": "postgresql+asyncpg",
}


def async_database_url(value: str | Path) -> URL:
    """把公开的标准数据库 URL 转换为对应 SQLAlchemy 异步驱动 URL。"""
    if isinstance(value, Path):
        value.parent.mkdir(parents=True, exist_ok=True)
        url = URL.create("sqlite", database=str(value))
    else:
        url = make_url(value)
    driver = ASYNC_DRIVERS.get(url.drivername)
    if driver is None:
        raise ValueError(f"不支持的数据库类型: {url.drivername}")
    return url.set(drivername=driver)


@dataclass(frozen=True, slots=True)
class MessageMapping:
    """一条 OneBot 消息与一组 Telegram 消息之间的长期关系。"""

    q_group_id: int
    q_message_ids: tuple[int, ...]
    tg_chat_id: int
    tg_message_ids: tuple[int, ...]
    q_user_id: int | None
    tg_user_id: int | None
    expires_at: float


class Sql:
    """通过 SQLAlchemy Async 持久化群绑定和 30 天消息映射。"""

    def __init__(self, database: str | Path | None = None) -> None:
        self._url = async_database_url(database or config.database_url)
        self._engine: AsyncEngine | None = None
        # SQLite 需要串行化 BEGIN IMMEDIATE；也避免单实例内冲突写入无谓重试。
        self._write_lock = asyncio.Lock()
        self._is_sqlite = self._url.get_backend_name() == "sqlite"

    async def bind_group(self, q_group_id: int, tg_chat_id: int) -> None:
        """建立严格一对一的群绑定，已有冲突时拒绝静默覆盖。"""
        if q_group_id <= 0:
            raise ValueError("OneBot 群号必须是正整数")

        async with self._write_lock:
            try:
                async with self._transaction() as connection:
                    await connection.execute(
                        sa.insert(group_mappings).values(
                            q_group_id=q_group_id,
                            tg_chat_id=tg_chat_id,
                        )
                    )
                return
            except IntegrityError:
                pass

            async with self._connection() as connection:
                rows = (
                    await connection.execute(
                        sa.select(
                            group_mappings.c.q_group_id,
                            group_mappings.c.tg_chat_id,
                        ).where(
                            sa.or_(
                                group_mappings.c.q_group_id == q_group_id,
                                group_mappings.c.tg_chat_id == tg_chat_id,
                            )
                        )
                    )
                ).all()
            current_tg = next((row.tg_chat_id for row in rows if row.q_group_id == q_group_id), None)
            current_q = next((row.q_group_id for row in rows if row.tg_chat_id == tg_chat_id), None)
            if current_tg == tg_chat_id and current_q == q_group_id:
                return
            if current_tg is not None:
                raise ValueError(f"OneBot 群 {q_group_id} 已绑定其他 Telegram 群")
            if current_q is not None:
                raise ValueError(f"当前 Telegram 群已绑定 OneBot 群 {current_q}")
            raise RuntimeError("群绑定并发写入失败，请重试")

    async def unbind_tg_group(self, tg_chat_id: int) -> int | None:
        """按 Telegram 群解除绑定，并返回此前绑定的 OneBot 群号。"""
        async with self._write_lock, self._transaction(immediate=True) as connection:
            statement = sa.select(group_mappings.c.q_group_id).where(
                group_mappings.c.tg_chat_id == tg_chat_id
            )
            if not self._is_sqlite:
                statement = statement.with_for_update()
            q_group_id = (await connection.execute(statement)).scalar_one_or_none()
            if q_group_id is not None:
                await connection.execute(
                    sa.delete(group_mappings).where(
                        group_mappings.c.tg_chat_id == tg_chat_id
                    )
                )
            return q_group_id

    async def get_tg_group(self, q_group_id: int) -> int | None:
        return await self._lookup_group_column(
            group_mappings.c.tg_chat_id,
            group_mappings.c.q_group_id,
            q_group_id,
        )

    async def get_q_group(self, tg_chat_id: int) -> int | None:
        return await self._lookup_group_column(
            group_mappings.c.q_group_id,
            group_mappings.c.tg_chat_id,
            tg_chat_id,
        )

    async def get_setting(self, key: str) -> str | None:
        """读取应用级持久化配置。"""
        async with self._connection() as connection:
            return (
                await connection.execute(
                    sa.select(application_settings.c.value).where(
                        application_settings.c.key == key
                    )
                )
            ).scalar_one_or_none()

    async def set_setting(self, key: str, value: str) -> None:
        """以跨数据库兼容的更新后插入方式保存应用级配置。"""
        async with self._write_lock, self._transaction() as connection:
            result = await connection.execute(
                sa.update(application_settings)
                .where(application_settings.c.key == key)
                .values(value=value)
            )
            if not result.rowcount:
                await connection.execute(
                    sa.insert(application_settings).values(key=key, value=value)
                )

    async def set_tg_forward_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        return await self._set_group_flag(
            tg_chat_id,
            group_mappings.c.tg_forward_enabled,
            enabled,
        )

    async def get_tg_forward_enabled(self, tg_chat_id: int) -> bool | None:
        return await self._get_group_flag(
            tg_chat_id,
            group_mappings.c.tg_forward_enabled,
        )

    async def set_bot_forward_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        return await self._set_group_flag(
            tg_chat_id,
            group_mappings.c.bot_forward_enabled,
            enabled,
        )

    async def get_bot_forward_enabled(self, tg_chat_id: int) -> bool | None:
        return await self._get_group_flag(
            tg_chat_id,
            group_mappings.c.bot_forward_enabled,
        )

    async def set_id_show_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        return await self._set_group_flag(
            tg_chat_id,
            group_mappings.c.id_show_enabled,
            enabled,
        )

    async def get_id_show_enabled(self, tg_chat_id: int) -> bool | None:
        return await self._get_group_flag(
            tg_chat_id,
            group_mappings.c.id_show_enabled,
        )

    async def set_message_mapping(
        self,
        q_group_id: int,
        q_message_ids: tuple[int, ...],
        tg_chat_id: int,
        tg_message_ids: tuple[int, ...],
        q_user_id: int | None = None,
        tg_user_id: int | None = None,
        *,
        replace_older_than: float | None = None,
    ) -> bool:
        """保存 30 天有效的消息映射，并建立两侧全部 ID 的反向索引。"""
        if not q_message_ids:
            raise ValueError("OneBot 消息 ID 不能为空")
        if not tg_message_ids:
            raise ValueError("Telegram 消息 ID 不能为空")
        if len(set(q_message_ids)) != len(q_message_ids):
            raise ValueError("OneBot 消息 ID 不能重复")
        if len(set(tg_message_ids)) != len(tg_message_ids):
            raise ValueError("Telegram 消息 ID 不能重复")

        async with self._write_lock, self._transaction(immediate=True) as connection:
            q_conflicts = sa.select(onebot_message_mappings.c.mapping_id).where(
                onebot_message_mappings.c.q_group_id == q_group_id,
                onebot_message_mappings.c.q_message_id.in_(q_message_ids),
            )
            tg_conflicts = sa.select(telegram_message_mappings.c.mapping_id).where(
                telegram_message_mappings.c.tg_chat_id == tg_chat_id,
                telegram_message_mappings.c.tg_message_id.in_(tg_message_ids),
            )
            conflicts = set((await connection.execute(q_conflicts.union(tg_conflicts))).scalars())
            if conflicts:
                if replace_older_than is not None:
                    newest_expiry = await connection.scalar(
                        sa.select(sa.func.max(message_mappings.c.expires_at)).where(
                            message_mappings.c.id.in_(conflicts)
                        )
                    )
                    if (
                        newest_expiry is not None
                        and newest_expiry > replace_older_than + MESSAGE_RETENTION
                    ):
                        return False
                await connection.execute(
                    sa.delete(message_mappings).where(message_mappings.c.id.in_(conflicts))
                )

            result = await connection.execute(
                sa.insert(message_mappings).values(
                    q_group_id=q_group_id,
                    q_message_id=q_message_ids[-1],
                    tg_chat_id=tg_chat_id,
                    q_user_id=q_user_id,
                    tg_user_id=tg_user_id,
                    expires_at=time.time() + MESSAGE_RETENTION,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError("数据库未返回消息映射 ID")
            mapping_id = primary_key[0]
            await connection.execute(
                sa.insert(onebot_message_mappings),
                [
                    {
                        "mapping_id": mapping_id,
                        "q_group_id": q_group_id,
                        "q_message_id": message_id,
                        "position": position,
                    }
                    for position, message_id in enumerate(q_message_ids)
                ],
            )
            await connection.execute(
                sa.insert(telegram_message_mappings),
                [
                    {
                        "mapping_id": mapping_id,
                        "tg_chat_id": tg_chat_id,
                        "tg_message_id": message_id,
                    }
                    for message_id in tg_message_ids
                ],
            )
        return True

    async def get_tg_message(
        self,
        q_group_id: int,
        q_message_id: int,
    ) -> MessageMapping | None:
        return await self._lookup_message(
            onebot_message_mappings,
            onebot_message_mappings.c.q_group_id == q_group_id,
            onebot_message_mappings.c.q_message_id == q_message_id,
        )

    async def get_q_message(
        self,
        tg_chat_id: int,
        tg_message_id: int,
    ) -> MessageMapping | None:
        return await self._lookup_message(
            telegram_message_mappings,
            telegram_message_mappings.c.tg_chat_id == tg_chat_id,
            telegram_message_mappings.c.tg_message_id == tg_message_id,
        )

    async def purge_expired(self) -> None:
        async with self._write_lock, self._transaction() as connection:
            await connection.execute(
                sa.delete(message_mappings).where(
                    message_mappings.c.expires_at <= time.time()
                )
            )

    async def load(self) -> None:
        """自动迁移 schema，并创建异步连接池。"""
        if self._engine is not None:
            raise RuntimeError("消息映射数据库已经加载")
        if self._is_sqlite and self._url.database:
            Path(self._url.database).parent.mkdir(parents=True, exist_ok=True)
        await await_completion_on_cancel(
            asyncio.to_thread(migrate_database, self._url)
        )
        engine = create_async_engine(self._url, pool_pre_ping=True)
        if self._is_sqlite:
            event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
        self._engine = engine
        try:
            if self._is_sqlite:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql("PRAGMA foreign_keys = ON")
                    await connection.exec_driver_sql("PRAGMA synchronous = NORMAL")
                    mode = (await connection.exec_driver_sql("PRAGMA journal_mode = WAL")).scalar()
                    if str(mode).lower() != "wal":
                        raise RuntimeError("SQLite 无法启用 WAL 模式")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._engine is None:
            return
        engine = self._engine
        self._engine = None
        await engine.dispose()

    async def _lookup_group_column(
        self,
        select_column: sa.Column[int],
        key_column: sa.Column[int],
        key_value: int,
    ) -> int | None:
        async with self._connection() as connection:
            return (
                await connection.execute(
                    sa.select(select_column).where(key_column == key_value)
                )
            ).scalar_one_or_none()

    async def _lookup_message(
        self,
        side: sa.Table,
        *conditions: sa.ColumnElement[bool],
    ) -> MessageMapping | None:
        statement = (
            sa.select(message_mappings)
            .join(side, side.c.mapping_id == message_mappings.c.id)
            .where(*conditions, message_mappings.c.expires_at > time.time())
        )
        async with self._connection() as connection:
            row = (await connection.execute(statement)).mappings().one_or_none()
            return await self._mapping_from_row(connection, row)

    async def _set_group_flag(
        self,
        tg_chat_id: int,
        column: sa.Column[bool],
        enabled: bool,
    ) -> bool:
        async with self._write_lock, self._transaction() as connection:
            result = await connection.execute(
                sa.update(group_mappings)
                .where(group_mappings.c.tg_chat_id == tg_chat_id)
                .values({column.key: enabled})
            )
            return bool(result.rowcount)

    async def _get_group_flag(
        self,
        tg_chat_id: int,
        column: sa.Column[bool],
    ) -> bool | None:
        async with self._connection() as connection:
            return (
                await connection.execute(
                    sa.select(column).where(group_mappings.c.tg_chat_id == tg_chat_id)
                )
            ).scalar_one_or_none()

    async def _mapping_from_row(
        self,
        connection: AsyncConnection,
        row: sa.RowMapping | None,
    ) -> MessageMapping | None:
        if row is None:
            return None
        mapping_id = row["id"]
        q_message_ids = tuple(
            (
                await connection.execute(
                    sa.select(onebot_message_mappings.c.q_message_id)
                    .where(onebot_message_mappings.c.mapping_id == mapping_id)
                    .order_by(onebot_message_mappings.c.position)
                )
            ).scalars()
        )
        tg_message_ids = tuple(
            (
                await connection.execute(
                    sa.select(telegram_message_mappings.c.tg_message_id)
                    .where(telegram_message_mappings.c.mapping_id == mapping_id)
                    .order_by(telegram_message_mappings.c.tg_message_id)
                )
            ).scalars()
        )
        return MessageMapping(
            q_group_id=row["q_group_id"],
            q_message_ids=q_message_ids or (row["q_message_id"],),
            tg_chat_id=row["tg_chat_id"],
            tg_message_ids=tg_message_ids,
            q_user_id=row["q_user_id"],
            tg_user_id=row["tg_user_id"],
            expires_at=row["expires_at"],
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection]:
        engine = self._require_engine()
        async with engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def _transaction(self, *, immediate: bool = False) -> AsyncIterator[AsyncConnection]:
        engine = self._require_engine()
        async with engine.connect() as connection:
            if immediate and self._is_sqlite:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()
            else:
                async with connection.begin():
                    yield connection

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("消息映射数据库尚未加载")
        return self._engine


sql = Sql()


def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    """为连接池中的每条 SQLite 连接启用一致的约束和耐久性设置。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
    finally:
        cursor.close()
