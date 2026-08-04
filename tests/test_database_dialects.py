from pathlib import Path

from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from src.database_schema import group_mappings, message_mappings
from src.sql import async_database_url


class TestDatabaseDialects:
    def test_public_urls_select_async_drivers(self) -> None:
        assert async_database_url("sqlite:////tmp/q2tg.db").drivername == "sqlite+aiosqlite"
        assert (
            async_database_url("mysql://user:password@localhost:3306/q2tg").drivername
            == "mysql+asyncmy"
        )
        assert (
            async_database_url(
                "postgresql://user:password@localhost:5432/q2tg"
            ).drivername
            == "postgresql+asyncpg"
        )
        assert async_database_url(Path("/tmp/q2tg.db")).drivername == "sqlite+aiosqlite"

    def test_schema_compiles_for_all_supported_dialects(self) -> None:
        sqlite_ddl = str(CreateTable(message_mappings).compile(dialect=sqlite.dialect()))
        mysql_ddl = str(CreateTable(message_mappings).compile(dialect=mysql.dialect()))
        postgres_ddl = str(
            CreateTable(message_mappings).compile(dialect=postgresql.dialect())
        )
        mysql_groups = str(CreateTable(group_mappings).compile(dialect=mysql.dialect()))
        postgres_groups = str(
            CreateTable(group_mappings).compile(dialect=postgresql.dialect())
        )

        assert "id INTEGER NOT NULL" in sqlite_ddl
        assert "id BIGINT NOT NULL AUTO_INCREMENT" in mysql_ddl
        assert "id BIGSERIAL NOT NULL" in postgres_ddl
        assert "q_group_id BIGINT" in mysql_groups
        assert "tg_forward_enabled BOOL" in mysql_groups
        assert "q_group_id BIGINT" in postgres_groups
        assert "tg_forward_enabled BOOLEAN" in postgres_groups
