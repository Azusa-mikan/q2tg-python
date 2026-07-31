import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.sql import Sql


class SqlMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_failure_closes_connection(self) -> None:
        connection = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("pragma failed")),
            close=AsyncMock(),
        )
        with (
            TemporaryDirectory() as directory,
            patch("src.sql.aiosqlite.connect", new_callable=AsyncMock, return_value=connection),
        ):
            cache = Sql(Path(directory) / "cache.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "pragma failed"):
                await cache.load()

        connection.close.assert_awaited_once_with()
        with self.assertRaisesRegex(RuntimeError, "尚未加载"):
            cache._require_db()

    async def test_message_mapping_persists_and_supports_both_directions(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            cache = Sql(path)
            await cache.load()
            await cache.bind_group(123, -789)
            self.assertTrue(await cache.get_tg_forward_enabled(-789))
            self.assertTrue(await cache.set_tg_forward_enabled(-789, False))
            self.assertFalse(await cache.get_tg_forward_enabled(-789))
            self.assertTrue(await cache.get_id_show_enabled(-789))
            self.assertTrue(await cache.set_id_show_enabled(-789, False))
            self.assertFalse(await cache.get_id_show_enabled(-789))
            await cache.set_message_mapping(
                q_group_id=123,
                q_message_id=456,
                tg_chat_id=-789,
                tg_message_ids=(10, 11),
            )

            by_onebot = await cache.get_tg_message(123, 456)
            by_telegram = await cache.get_q_message(-789, 11)
            self.assertIsNotNone(by_onebot)
            self.assertIsNotNone(by_telegram)
            assert by_onebot is not None
            assert by_telegram is not None
            self.assertEqual(by_onebot.tg_message_ids, (10, 11))
            self.assertEqual(by_telegram.q_message_id, 456)
            self.assertGreater(by_onebot.expires_at - time.time(), 29 * 24 * 60 * 60)
            await cache.close()

            reopened = Sql(path)
            await reopened.load()
            persisted = await reopened.get_tg_message(123, 456)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.tg_message_ids, (10, 11))
            self.assertEqual(await reopened.get_tg_group(123), -789)
            self.assertFalse(await reopened.get_tg_forward_enabled(-789))
            self.assertFalse(await reopened.get_id_show_enabled(-789))
            await reopened.unbind_tg_group(-789)
            await reopened.bind_group(123, -789)
            self.assertTrue(await reopened.get_id_show_enabled(-789))
            await reopened.close()

            with closing(sqlite3.connect(path)) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(journal_mode, ("wal",))


if __name__ == "__main__":
    unittest.main()
