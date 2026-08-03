import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.sql import Sql


class SqlMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_setting_persists_and_updates(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.sqlite3"
            database = Sql(path)
            await database.load()
            self.assertIsNone(await database.get_setting("telegraph_access_token"))
            await database.set_setting("telegraph_access_token", "first-test-token")
            await database.set_setting("telegraph_access_token", "second-test-token")
            await database.close()

            reopened = Sql(path)
            await reopened.load()
            try:
                self.assertEqual(
                    await reopened.get_setting("telegraph_access_token"),
                    "second-test-token",
                )
            finally:
                await reopened.close()

    async def test_initialization_failure_closes_connection(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "src.sql.migrate_database",
            side_effect=RuntimeError("migration failed"),
        ):
            cache = Sql(Path(directory) / "cache.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                await cache.load()

        with self.assertRaisesRegex(RuntimeError, "尚未加载"):
            await cache.get_tg_group(123)

    async def test_message_mapping_persists_and_supports_both_directions(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            cache = Sql(path)
            await cache.load()
            await cache.bind_group(123, -789)
            self.assertTrue(await cache.get_tg_forward_enabled(-789))
            self.assertFalse(await cache.get_bot_forward_enabled(-789))
            self.assertTrue(await cache.set_bot_forward_enabled(-789, True))
            self.assertTrue(await cache.get_bot_forward_enabled(-789))
            self.assertTrue(await cache.set_tg_forward_enabled(-789, False))
            self.assertFalse(await cache.get_tg_forward_enabled(-789))
            self.assertTrue(await cache.get_id_show_enabled(-789))
            self.assertTrue(await cache.set_id_show_enabled(-789, False))
            self.assertFalse(await cache.get_id_show_enabled(-789))
            await cache.set_message_mapping(
                q_group_id=123,
                q_message_ids=(455, 456),
                tg_chat_id=-789,
                tg_message_ids=(10, 11),
            )

            by_onebot = await cache.get_tg_message(123, 456)
            by_first_onebot = await cache.get_tg_message(123, 455)
            by_telegram = await cache.get_q_message(-789, 11)
            self.assertIsNotNone(by_onebot)
            self.assertIsNotNone(by_telegram)
            self.assertIsNotNone(by_first_onebot)
            assert by_onebot is not None
            assert by_telegram is not None
            self.assertEqual(by_onebot.tg_message_ids, (10, 11))
            self.assertEqual(by_telegram.q_message_ids, (455, 456))
            assert by_first_onebot is not None
            self.assertEqual(by_first_onebot.tg_message_ids, (10, 11))
            self.assertGreater(by_onebot.expires_at - time.time(), 29 * 24 * 60 * 60)
            await cache.close()

            reopened = Sql(path)
            await reopened.load()
            persisted = await reopened.get_tg_message(123, 456)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.tg_message_ids, (10, 11))
            self.assertEqual(persisted.q_message_ids, (455, 456))
            self.assertEqual(await reopened.get_tg_group(123), -789)
            self.assertFalse(await reopened.get_tg_forward_enabled(-789))
            self.assertTrue(await reopened.get_bot_forward_enabled(-789))
            self.assertFalse(await reopened.get_id_show_enabled(-789))
            await reopened.unbind_tg_group(-789)
            await reopened.bind_group(123, -789)
            self.assertFalse(await reopened.get_bot_forward_enabled(-789))
            self.assertTrue(await reopened.get_id_show_enabled(-789))
            await reopened.close()

            with closing(sqlite3.connect(path)) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(journal_mode, ("wal",))

    async def test_existing_single_onebot_mapping_is_migrated(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE group_mappings (
                        q_group_id INTEGER PRIMARY KEY,
                        tg_chat_id INTEGER NOT NULL UNIQUE,
                        tg_forward_enabled INTEGER NOT NULL DEFAULT 1
                            CHECK (tg_forward_enabled IN (0, 1)),
                        id_show_enabled INTEGER NOT NULL DEFAULT 1
                            CHECK (id_show_enabled IN (0, 1))
                    );
                    CREATE TABLE message_mappings (
                        id INTEGER PRIMARY KEY,
                        q_group_id INTEGER NOT NULL,
                        q_message_id INTEGER NOT NULL,
                        tg_chat_id INTEGER NOT NULL,
                        q_user_id INTEGER,
                        tg_user_id INTEGER,
                        expires_at REAL NOT NULL,
                        UNIQUE (q_group_id, q_message_id)
                    );
                    CREATE TABLE telegram_message_mappings (
                        mapping_id INTEGER NOT NULL REFERENCES message_mappings(id) ON DELETE CASCADE,
                        tg_chat_id INTEGER NOT NULL,
                        tg_message_id INTEGER NOT NULL,
                        PRIMARY KEY (tg_chat_id, tg_message_id)
                    );
                    INSERT INTO message_mappings VALUES (1, 123, 456, -789, NULL, NULL, 9999999999);
                    INSERT INTO telegram_message_mappings VALUES (1, -789, 10);
                    """
                )

            cache = Sql(path)
            await cache.load()
            try:
                mapping = await cache.get_tg_message(123, 456)
                assert mapping is not None
                self.assertEqual(mapping.q_message_ids, (456,))
                self.assertEqual(mapping.tg_message_ids, (10,))
            finally:
                await cache.close()

            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
