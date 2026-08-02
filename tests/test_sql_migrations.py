import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.sql import Sql
from src.sql_migrations import HEAD_REVISION


class SqlMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_database_is_adopted_without_rebuilding(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "current.sqlite3"
            database = Sql(path)
            await database.load()
            await database.bind_group(123, -789)
            await database.set_message_mapping(
                q_group_id=123,
                q_message_ids=(456, 457),
                tg_chat_id=-789,
                tg_message_ids=(10,),
            )
            await database.close()

            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP TABLE alembic_version")
                connection.commit()

            adopted = Sql(path)
            await adopted.load()
            try:
                mapping = await adopted.get_tg_message(123, 457)
                self.assertIsNotNone(mapping)
                assert mapping is not None
                self.assertEqual(mapping.q_message_ids, (456, 457))
            finally:
                await adopted.close()

            with closing(sqlite3.connect(path)) as connection:
                revision = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                user_version = connection.execute("PRAGMA user_version").fetchone()
            self.assertEqual(revision, (HEAD_REVISION,))
            self.assertEqual(user_version, (1,))

    async def test_incomplete_schema_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "broken.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE message_mappings (id INTEGER PRIMARY KEY)"
                )
                connection.commit()

            database = Sql(path)
            with self.assertRaisesRegex(RuntimeError, "无法识别 SQLite schema"):
                await database.load()

            with closing(sqlite3.connect(path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("alembic_version", tables)

    async def test_unknown_user_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version = 2")
                connection.commit()

            database = Sql(path)
            with self.assertRaisesRegex(RuntimeError, "无法识别 SQLite 数据库版本 2"):
                await database.load()
