"""使用 SQLite 保存群绑定和长期消息映射。"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

MESSAGE_RETENTION = 30 * 24 * 60 * 60

DATABASE_PATH = Path(__file__).parents[1] / "data" / "q2tg.db"


@dataclass(frozen=True, slots=True)
class MessageMapping:
    """一条 OneBot 消息与一组 Telegram 消息之间的长期关系。

    Telegram 相册包含多个 message_id，因此使用元组表示一对多；普通文本和
    单图是只有一个元素的特例。群 ID 是键的一部分，避免不同群中的消息 ID
    发生碰撞。
    """

    q_group_id: int
    q_message_id: int
    tg_chat_id: int
    tg_message_ids: tuple[int, ...]
    q_user_id: int | None
    tg_user_id: int | None
    expires_at: float


class Sql:
    """通过 aiosqlite 持久化群绑定和 30 天消息映射。"""

    def __init__(self, database_path: Path = DATABASE_PATH) -> None:
        self._database_path = database_path
        self._db: aiosqlite.Connection | None = None
        # aiosqlite 会串行执行单条语句，但不会自动保护由多条语句组成的事务。
        self._db_lock = asyncio.Lock()

    async def bind_group(self, q_group_id: int, tg_chat_id: int) -> None:
        """建立严格一对一的群绑定，已有冲突时拒绝静默覆盖。"""
        if q_group_id <= 0:
            raise ValueError("OneBot 群号必须是正整数")

        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                "SELECT q_group_id, tg_chat_id FROM group_mappings "
                "WHERE q_group_id = ? OR tg_chat_id = ?",
                (q_group_id, tg_chat_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            current_tg = next((row[1] for row in rows if row[0] == q_group_id), None)
            current_q = next((row[0] for row in rows if row[1] == tg_chat_id), None)
            if current_tg == tg_chat_id and current_q == q_group_id:
                # 完全相同的重复绑定视为幂等成功。
                return
            if current_tg is not None:
                raise ValueError(f"OneBot 群 {q_group_id} 已绑定其他 Telegram 群")
            if current_q is not None:
                raise ValueError(f"当前 Telegram 群已绑定 OneBot 群 {current_q}")

            await db.execute(
                "INSERT INTO group_mappings (q_group_id, tg_chat_id) VALUES (?, ?)",
                (q_group_id, tg_chat_id),
            )
            await db.commit()

    async def unbind_tg_group(self, tg_chat_id: int) -> int | None:
        """按 Telegram 群解除绑定，并返回此前绑定的 OneBot 群号。"""
        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                "DELETE FROM group_mappings WHERE tg_chat_id = ? RETURNING q_group_id",
                (tg_chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
        return row[0] if row is not None else None

    async def get_tg_group(self, q_group_id: int) -> int | None:
        """查询 OneBot 群绑定的 Telegram chat_id。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT tg_chat_id FROM group_mappings WHERE q_group_id = ?",
            (q_group_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row is not None else None

    async def get_q_group(self, tg_chat_id: int) -> int | None:
        """查询 Telegram 群绑定的 OneBot group_id。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT q_group_id FROM group_mappings WHERE tg_chat_id = ?",
            (tg_chat_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row is not None else None

    async def set_tg_forward_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        """设置当前 Telegram 群到 OneBot 的转发开关，返回群是否已绑定。"""
        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                "UPDATE group_mappings SET tg_forward_enabled = ? WHERE tg_chat_id = ?",
                (int(enabled), tg_chat_id),
            )
            changed = cursor.rowcount > 0
            await cursor.close()
            await db.commit()
        return changed

    async def get_tg_forward_enabled(self, tg_chat_id: int) -> bool | None:
        """查询 Telegram 到 OneBot 的转发状态；未绑定群返回 None。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT tg_forward_enabled FROM group_mappings WHERE tg_chat_id = ?",
            (tg_chat_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row[0]) if row is not None else None

    async def set_id_show_enabled(self, tg_chat_id: int, enabled: bool) -> bool:
        """设置无名 Onebot 用户是否显示数字 ID，返回群是否已绑定。"""
        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                "UPDATE group_mappings SET id_show_enabled = ? WHERE tg_chat_id = ?",
                (int(enabled), tg_chat_id),
            )
            changed = cursor.rowcount > 0
            await cursor.close()
            await db.commit()
            return changed

    async def get_id_show_enabled(self, tg_chat_id: int) -> bool | None:
        """查询无名用户 ID 显示设置；已绑定群默认开启，未绑定群返回 None。"""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT id_show_enabled FROM group_mappings WHERE tg_chat_id = ?",
            (tg_chat_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row[0]) if row is not None else None

    async def set_message_mapping(
        self,
        q_group_id: int,
        q_message_id: int,
        tg_chat_id: int,
        tg_message_ids: tuple[int, ...],
        q_user_id: int | None = None,
        tg_user_id: int | None = None,
    ) -> None:
        """保存 30 天有效的消息映射，并建立所有 Telegram ID 的反向索引。"""
        if not tg_message_ids:
            raise ValueError("Telegram 消息 ID 不能为空")

        db = self._require_db()
        expires_at = time.time() + MESSAGE_RETENTION
        placeholders = ",".join("?" for _ in tg_message_ids)

        # 任意一侧 ID 已存在时删除整条旧映射，避免留下相互矛盾的回复关系。
        async with self._db_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "DELETE FROM message_mappings WHERE q_group_id = ? AND q_message_id = ?",
                    (q_group_id, q_message_id),
                )
                cursor = await db.execute(
                    f"""
                    SELECT DISTINCT mapping_id
                    FROM telegram_message_mappings
                    WHERE tg_chat_id = ? AND tg_message_id IN ({placeholders})
                    """,
                    (tg_chat_id, *tg_message_ids),
                )
                conflicting_ids = [row[0] for row in await cursor.fetchall()]
                await cursor.close()
                if conflicting_ids:
                    conflict_placeholders = ",".join("?" for _ in conflicting_ids)
                    await db.execute(
                        f"DELETE FROM message_mappings WHERE id IN ({conflict_placeholders})",
                        conflicting_ids,
                    )

                cursor = await db.execute(
                    """
                    INSERT INTO message_mappings (
                        q_group_id, q_message_id, tg_chat_id, q_user_id, tg_user_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                (
                    q_group_id,
                    q_message_id,
                    tg_chat_id,
                    q_user_id,
                    tg_user_id,
                    expires_at,
                ),
                )
                mapping_id = cursor.lastrowid
                await cursor.close()
                if mapping_id is None:
                    raise RuntimeError("SQLite 未返回消息映射 ID")
                await db.executemany(
                    """
                    INSERT INTO telegram_message_mappings (mapping_id, tg_chat_id, tg_message_id)
                    VALUES (?, ?, ?)
                    """,
                    ((mapping_id, tg_chat_id, message_id) for message_id in tg_message_ids),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_tg_message(
        self,
        q_group_id: int,
        q_message_id: int,
    ) -> MessageMapping | None:
        """通过 OneBot 群和消息 ID 查询对应的 Telegram 消息组。"""
        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                """
                SELECT id, q_group_id, q_message_id, tg_chat_id,
                       q_user_id, tg_user_id, expires_at
                FROM message_mappings
                WHERE q_group_id = ? AND q_message_id = ? AND expires_at > ?
                """,
                (q_group_id, q_message_id, time.time()),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return await self._mapping_from_row(row)

    async def get_q_message(
        self,
        tg_chat_id: int,
        tg_message_id: int,
    ) -> MessageMapping | None:
        """通过相册中的任意 Telegram 消息 ID 查询 OneBot 消息。"""
        db = self._require_db()
        async with self._db_lock:
            cursor = await db.execute(
                """
                SELECT m.id, m.q_group_id, m.q_message_id, m.tg_chat_id,
                       m.q_user_id, m.tg_user_id, m.expires_at
                FROM message_mappings AS m
                JOIN telegram_message_mappings AS t ON t.mapping_id = m.id
                WHERE t.tg_chat_id = ? AND t.tg_message_id = ? AND m.expires_at > ?
                """,
                (tg_chat_id, tg_message_id, time.time()),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return await self._mapping_from_row(row)

    async def purge_expired(self) -> None:
        """删除已过期的消息映射；反向索引由外键级联删除。"""
        db = self._require_db()
        async with self._db_lock:
            await db.execute("DELETE FROM message_mappings WHERE expires_at <= ?", (time.time(),))
            await db.commit()

    async def load(self) -> None:
        """连接 SQLite，并创建群绑定和消息映射所需的数据表。"""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._database_path)
        try:
            cursor = await self._db.execute("PRAGMA journal_mode = WAL")
            journal_mode = await cursor.fetchone()
            await cursor.close()
            if journal_mode is None or journal_mode[0].lower() != "wal":
                raise RuntimeError("SQLite 无法启用 WAL 模式")
            await self._db.execute("PRAGMA synchronous = NORMAL")
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS group_mappings (
                    q_group_id INTEGER PRIMARY KEY,
                    tg_chat_id INTEGER NOT NULL UNIQUE,
                    tg_forward_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (tg_forward_enabled IN (0, 1)),
                    id_show_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (id_show_enabled IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS message_mappings (
                    id INTEGER PRIMARY KEY,
                    q_group_id INTEGER NOT NULL,
                    q_message_id INTEGER NOT NULL,
                    tg_chat_id INTEGER NOT NULL,
                    q_user_id INTEGER,
                    tg_user_id INTEGER,
                    expires_at REAL NOT NULL,
                    UNIQUE (q_group_id, q_message_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_message_mappings (
                    mapping_id INTEGER NOT NULL REFERENCES message_mappings(id) ON DELETE CASCADE,
                    tg_chat_id INTEGER NOT NULL,
                    tg_message_id INTEGER NOT NULL,
                    PRIMARY KEY (tg_chat_id, tg_message_id)
                );

                CREATE INDEX IF NOT EXISTS message_mappings_expires_at
                ON message_mappings(expires_at);
                """
            )
            await self._db.commit()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """关闭 SQLite 连接。"""
        if self._db is None:
            return
        await self._db.close()
        self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("消息映射数据库尚未加载")
        return self._db

    async def _mapping_from_row(self, row: aiosqlite.Row | tuple | None) -> MessageMapping | None:
        if row is None:
            return None
        (
            mapping_id,
            q_group_id,
            q_message_id,
            tg_chat_id,
            q_user_id,
            tg_user_id,
            expires_at,
        ) = row
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT tg_message_id
            FROM telegram_message_mappings
            WHERE mapping_id = ?
            ORDER BY tg_message_id
            """,
            (mapping_id,),
        )
        tg_message_ids = tuple(item[0] for item in await cursor.fetchall())
        await cursor.close()
        return MessageMapping(
            q_group_id=q_group_id,
            q_message_id=q_message_id,
            tg_chat_id=tg_chat_id,
            tg_message_ids=tg_message_ids,
            q_user_id=q_user_id,
            tg_user_id=tg_user_id,
            expires_at=expires_at,
        )

# 所有入口和转发函数共享同一个 SQLite 管理实例。
sql = Sql()
