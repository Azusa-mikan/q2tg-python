import os
import unittest

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from src.sql import Sql, async_database_url


@unittest.skipUnless(
    os.getenv("Q2TG_TEST_DATABASE_URL"),
    "未配置 Q2TG_TEST_DATABASE_URL",
)
class DatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.url = os.environ["Q2TG_TEST_DATABASE_URL"]
        async_url = async_database_url(self.url)
        engine = create_async_engine(async_url)
        async with engine.begin() as connection:
            for table in (
                "onebot_message_mappings",
                "telegram_message_mappings",
                "message_mappings",
                "group_mappings",
                "alembic_version",
            ):
                await connection.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
        await engine.dispose()

    async def test_repository_contract(self) -> None:
        database = Sql(self.url)
        await database.load()
        try:
            q_group_id = 4_000_000_001
            tg_chat_id = -1_000_000_000_001
            await database.bind_group(q_group_id, tg_chat_id)
            await database.bind_group(q_group_id, tg_chat_id)
            self.assertEqual(await database.get_tg_group(q_group_id), tg_chat_id)
            self.assertEqual(await database.get_q_group(tg_chat_id), q_group_id)
            self.assertTrue(await database.get_tg_forward_enabled(tg_chat_id))
            self.assertTrue(await database.set_tg_forward_enabled(tg_chat_id, False))
            self.assertTrue(await database.set_tg_forward_enabled(tg_chat_id, False))
            self.assertFalse(await database.get_tg_forward_enabled(tg_chat_id))

            await database.set_message_mapping(
                q_group_id=q_group_id,
                q_message_ids=(5_000_000_001, 5_000_000_002),
                tg_chat_id=tg_chat_id,
                tg_message_ids=(6_000_000_001, 6_000_000_002),
                q_user_id=7_000_000_001,
                tg_user_id=8_000_000_001,
            )
            mapping = await database.get_q_message(tg_chat_id, 6_000_000_002)
            self.assertIsNotNone(mapping)
            assert mapping is not None
            self.assertEqual(mapping.q_message_ids, (5_000_000_001, 5_000_000_002))
            self.assertEqual(mapping.tg_message_ids, (6_000_000_001, 6_000_000_002))
        finally:
            await database.close()

        reopened = Sql(self.url)
        await reopened.load()
        try:
            self.assertIsNotNone(
                await reopened.get_tg_message(q_group_id, 5_000_000_001)
            )
            self.assertEqual(await reopened.unbind_tg_group(tg_chat_id), q_group_id)
            self.assertIsNone(await reopened.unbind_tg_group(tg_chat_id))
        finally:
            await reopened.close()
