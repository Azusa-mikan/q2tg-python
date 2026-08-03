"""接入历史 SQLite schema，并使用 Alembic 自动升级数据库。"""

import asyncio
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from src.paths import PROJECT_ROOT

LEGACY_REVISION = "0001_legacy_schema"
CURRENT_REVISION = "0002_onebot_message_mappings"
INDEX_REVISION = "0003_mapping_id_index"
SETTINGS_REVISION = "0004_application_settings"
HEAD_REVISION = "0005_bot_forward"
LEGACY_TABLES = {
    "group_mappings",
    "message_mappings",
    "telegram_message_mappings",
}
CURRENT_TABLES = LEGACY_TABLES | {"onebot_message_mappings"}
HEAD_TABLES = CURRENT_TABLES | {"application_settings"}


def migrate_database(database_url: URL) -> None:
    """同步入口；由应用在线程中调用，内部运行异步数据库检查。"""
    tables, indexes, group_columns, user_version = asyncio.run(
        _inspect_database(database_url)
    )
    _upgrade_database(database_url, tables, indexes, group_columns, user_version)


async def _inspect_database(
    database_url: URL,
) -> tuple[set[str], set[str], set[str], int]:
    if database_url.get_backend_name() == "sqlite" and database_url.database:
        Path(database_url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        async with engine.connect() as connection:
            tables = set(await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
            indexes: set[str] = set()
            group_columns: set[str] = set()
            if "group_mappings" in tables:
                group_columns = await connection.run_sync(
                    lambda sync: {
                        column["name"]
                        for column in sa.inspect(sync).get_columns("group_mappings")
                    }
                )
            if "telegram_message_mappings" in tables:
                index_names = await connection.run_sync(
                    lambda sync: {
                        index["name"]
                        for index in sa.inspect(sync).get_indexes(
                            "telegram_message_mappings"
                        )
                    }
                )
                indexes = {name for name in index_names if name is not None}
            user_version = 0
            if database_url.get_backend_name() == "sqlite":
                user_version = int(
                    (await connection.exec_driver_sql("PRAGMA user_version")).scalar_one()
                )
    finally:
        await engine.dispose()
    return tables, indexes, group_columns, user_version


def _upgrade_database(
    database_url: URL,
    tables: set[str],
    indexes: set[str],
    group_columns: set[str],
    user_version: int,
) -> None:
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    config.attributes["configure_logger"] = False
    application_tables = tables & HEAD_TABLES

    if "alembic_version" in tables:
        pass
    elif not application_tables:
        if database_url.get_backend_name() == "sqlite" and user_version != 0:
            raise RuntimeError(f"无法识别 SQLite 数据库版本 {user_version}")
    elif database_url.get_backend_name() != "sqlite":
        raise RuntimeError("非空数据库缺少 Alembic 版本标记，拒绝自动接管")
    elif application_tables == LEGACY_TABLES and user_version == 0:
        command.stamp(config, LEGACY_REVISION)
    elif (
        application_tables == CURRENT_TABLES or application_tables == HEAD_TABLES
    ) and user_version == 1:
        if application_tables == HEAD_TABLES:
            revision = (
                HEAD_REVISION
                if "bot_forward_enabled" in group_columns
                else SETTINGS_REVISION
            )
        elif "telegram_message_mappings_mapping_id" in indexes:
            revision = INDEX_REVISION
        else:
            revision = CURRENT_REVISION
        command.stamp(config, revision)
    else:
        raise RuntimeError(
            f"无法识别 SQLite schema：user_version={user_version}, "
            f"tables={sorted(application_tables)}"
        )

    command.upgrade(config, "head")
