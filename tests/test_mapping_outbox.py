import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from src.mapping_outbox import MappingOutbox, PendingMessageMapping, database_values
from src.sql import Sql


def example_mapping() -> PendingMessageMapping:
    return PendingMessageMapping(
        q_group_id=810_001,
        q_message_ids=(810_002, 810_003),
        tg_chat_id=-810_004,
        tg_message_ids=(810_005,),
        q_user_id=810_006,
    )


@pytest.mark.asyncio
async def test_pending_mapping_survives_outbox_reload() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pending.json"
        database = cast(Sql, SimpleNamespace(set_message_mapping=AsyncMock()))
        outbox = MappingOutbox(database, path=path)
        await outbox.load()
        await outbox.enqueue(example_mapping())
        assert outbox.pending_count() == 1
        await outbox.close()

        reopened = MappingOutbox(database, path=path)
        await reopened.load()
        try:
            assert reopened.pending_count() == 1
        finally:
            await reopened.close()


@pytest.mark.asyncio
async def test_pending_mapping_retries_until_database_recovers() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pending.json"
        set_mapping = AsyncMock(
            side_effect=[RuntimeError("database unavailable"), None]
        )
        database = cast(
            Sql,
            SimpleNamespace(set_message_mapping=set_mapping),
        )
        outbox = MappingOutbox(database, path=path, retry_interval=0)
        await outbox.load()
        mapping = example_mapping()
        await outbox.enqueue(mapping)
        with patch("src.mapping_outbox.baselog.exception"):
            worker = asyncio.create_task(outbox.run())
            try:
                while outbox.pending_count():
                    await asyncio.sleep(0)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        assert set_mapping.await_count == 2
        assert set_mapping.await_args is not None
        assert set_mapping.await_args.kwargs["replace_older_than"] == mapping.created_at
        await outbox.close()


@pytest.mark.asyncio
async def test_new_pending_mapping_replaces_conflicting_old_record() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pending.json"
        database = cast(Sql, SimpleNamespace(set_message_mapping=AsyncMock()))
        old = example_mapping()
        new = PendingMessageMapping(
            q_group_id=old.q_group_id,
            q_message_ids=old.q_message_ids,
            tg_chat_id=old.tg_chat_id,
            tg_message_ids=(810_007,),
            q_user_id=old.q_user_id,
        )
        outbox = MappingOutbox(database, path=path)
        await outbox.load()
        await outbox.enqueue(old)
        await outbox.enqueue(new)

        assert outbox.pending_count() == 1
        assert outbox.get_tg_message(old.q_group_id, old.q_message_ids[0]) == new
        assert outbox.get_q_message(new.tg_chat_id, new.tg_message_ids[0]) == new
        await outbox.close()


@pytest.mark.asyncio
async def test_old_pending_mapping_does_not_overwrite_new_database_mapping() -> None:
    with TemporaryDirectory() as directory:
        database = Sql(Path(directory) / "mapping.sqlite3")
        await database.load()
        old = example_mapping()
        new = PendingMessageMapping(
            q_group_id=old.q_group_id,
            q_message_ids=old.q_message_ids,
            tg_chat_id=old.tg_chat_id,
            tg_message_ids=(810_008,),
            q_user_id=old.q_user_id,
        )
        await database.set_message_mapping(**database_values(new))
        outbox = MappingOutbox(
            database,
            path=Path(directory) / "pending.json",
            retry_interval=0,
        )
        await outbox.load()
        await outbox.enqueue(old)
        worker = asyncio.create_task(outbox.run())
        try:
            while outbox.pending_count():
                await asyncio.sleep(0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        mapping = await database.get_tg_message(new.q_group_id, new.q_message_ids[0])
        assert mapping is not None
        assert mapping.tg_message_ids == new.tg_message_ids
        await outbox.close()
        await database.close()


@pytest.mark.asyncio
async def test_new_pending_mapping_replaces_older_database_mapping() -> None:
    with TemporaryDirectory() as directory:
        database = Sql(Path(directory) / "mapping.sqlite3")
        await database.load()
        old = example_mapping()
        await database.set_message_mapping(**database_values(old))
        new = PendingMessageMapping(
            q_group_id=old.q_group_id,
            q_message_ids=old.q_message_ids,
            tg_chat_id=old.tg_chat_id,
            tg_message_ids=(810_009,),
            q_user_id=old.q_user_id,
            created_at=time.time() + 1,
        )
        outbox = MappingOutbox(
            database,
            path=Path(directory) / "pending.json",
            retry_interval=0,
        )
        await outbox.load()
        await outbox.enqueue(new)
        worker = asyncio.create_task(outbox.run())
        try:
            while outbox.pending_count():
                await asyncio.sleep(0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        mapping = await database.get_tg_message(new.q_group_id, new.q_message_ids[0])
        assert mapping is not None
        assert mapping.tg_message_ids == new.tg_message_ids
        await outbox.close()
        await database.close()


@pytest.mark.asyncio
async def test_worker_retries_after_outbox_confirmation_write_fails() -> None:
    with TemporaryDirectory() as directory:
        set_mapping = AsyncMock()
        database = cast(Sql, SimpleNamespace(set_message_mapping=set_mapping))
        outbox = MappingOutbox(
            database,
            path=Path(directory) / "pending.json",
            retry_interval=0,
        )
        await outbox.load()
        await outbox.enqueue(example_mapping())

        with (
            patch.object(
                outbox,
                "_write_payload",
                side_effect=[OSError("disk unavailable"), None],
            ),
            patch("src.mapping_outbox.baselog.exception") as log_exception,
        ):
            worker = asyncio.create_task(outbox.run())
            try:
                while outbox.pending_count():
                    await asyncio.sleep(0)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        assert set_mapping.await_count == 2
        log_exception.assert_called_once_with("待补偿消息映射确认失败")
        await outbox.close()
