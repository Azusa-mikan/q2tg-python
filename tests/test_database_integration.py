import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from src.sql import Sql, async_database_url


@pytest.mark.skipif(
    not os.getenv("Q2TG_TEST_DATABASE_URL"), reason="未配置 Q2TG_TEST_DATABASE_URL"
)
@pytest.mark.asyncio
class TestDatabaseIntegration:
    @pytest_asyncio.fixture(autouse=True)
    async def setup_database(self):
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
        yield

    async def test_repository_contract(self) -> None:
        database = Sql(self.url)
        await database.load()
        try:
            q_group_id = 4_000_000_001
            tg_chat_id = -1_000_000_000_001
            await database.bind_group(q_group_id, tg_chat_id)
            await database.bind_group(q_group_id, tg_chat_id)
            assert await database.get_tg_group(q_group_id) == tg_chat_id
            assert await database.get_q_group(tg_chat_id) == q_group_id
            assert await database.get_tg_forward_enabled(tg_chat_id)
            assert await database.set_tg_forward_enabled(tg_chat_id, False)
            assert await database.set_tg_forward_enabled(tg_chat_id, False)
            assert not await database.get_tg_forward_enabled(tg_chat_id)

            await database.set_message_mapping(
                q_group_id=q_group_id,
                q_message_ids=(5_000_000_001, 5_000_000_002),
                tg_chat_id=tg_chat_id,
                tg_message_ids=(6_000_000_001, 6_000_000_002),
                q_user_id=7_000_000_001,
                tg_user_id=8_000_000_001,
            )
            mapping = await database.get_q_message(tg_chat_id, 6_000_000_002)
            assert mapping is not None
            assert mapping.q_message_ids == (5_000_000_001, 5_000_000_002)
            assert mapping.tg_message_ids == (6_000_000_001, 6_000_000_002)
        finally:
            await database.close()

        reopened = Sql(self.url)
        await reopened.load()
        try:
            assert await reopened.get_tg_message(q_group_id, 5_000_000_001) is not None
            assert await reopened.unbind_tg_group(tg_chat_id) == q_group_id
            assert await reopened.unbind_tg_group(tg_chat_id) is None
        finally:
            await reopened.close()
