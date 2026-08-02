import unittest
from pathlib import Path

from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from src.database_schema import group_mappings, message_mappings
from src.sql import async_database_url


class DatabaseDialectTests(unittest.TestCase):
    def test_public_urls_select_async_drivers(self) -> None:
        self.assertEqual(
            async_database_url("sqlite:////tmp/q2tg.db").drivername,
            "sqlite+aiosqlite",
        )
        self.assertEqual(
            async_database_url("mysql://user:password@localhost:3306/q2tg").drivername,
            "mysql+asyncmy",
        )
        self.assertEqual(
            async_database_url(
                "postgresql://user:password@localhost:5432/q2tg"
            ).drivername,
            "postgresql+asyncpg",
        )
        self.assertEqual(
            async_database_url(Path("/tmp/q2tg.db")).drivername,
            "sqlite+aiosqlite",
        )

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

        self.assertIn("id INTEGER NOT NULL", sqlite_ddl)
        self.assertIn("id BIGINT NOT NULL AUTO_INCREMENT", mysql_ddl)
        self.assertIn("id BIGSERIAL NOT NULL", postgres_ddl)
        self.assertIn("q_group_id BIGINT", mysql_groups)
        self.assertIn("tg_forward_enabled BOOL", mysql_groups)
        self.assertIn("q_group_id BIGINT", postgres_groups)
        self.assertIn("tg_forward_enabled BOOLEAN", postgres_groups)
