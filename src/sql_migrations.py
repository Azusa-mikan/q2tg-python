"""SQLite schema 版本迁移。

SQLite 将 ``PRAGMA user_version`` 作为一个由应用自行解释的持久化整数保存
在数据库文件中。本项目用它表示已经成功执行的最新迁移编号：历史数据库没有
设置过该值，因此视为版本 0；当前迁移完成后为版本 1。

维护约定：

1. ``src.sql.Sql.load`` 只创建所有版本都依赖的基础表，然后调用本模块。
2. schema 发生版本化变更时，只能在 ``MIGRATIONS`` 末尾追加连续整数编号，
   并同步提高 ``LATEST_DATABASE_VERSION``。
3. 已经发布的迁移不得修改或删除。修正旧迁移也必须追加一个新版本，否则不同
   安装时间的数据库会得到不同 schema。
4. 每个迁移及其 ``user_version`` 更新位于同一事务中。迁移失败后版本号保持
   不变，下次启动会重新执行该版本。
5. 数据库版本高于当前程序支持版本时拒绝启动，避免旧程序误写新 schema。

例如新增版本 2 时，保留版本 1 的 SQL，增加 ``MIGRATIONS[2]``，并将
``LATEST_DATABASE_VERSION`` 改为 2。版本 0 会依次执行 1、2，版本 1 只执行 2。

以上注释可按需要更改。
"""

import aiosqlite

LATEST_DATABASE_VERSION = 1

# 键是迁移完成后的数据库版本，而不是应用版本。迁移必须从 1 开始连续编号。
MIGRATIONS: dict[int, str] = {
    1: """
        -- 旧 schema 只能为一个逻辑映射保存一个 OneBot message_id。
        -- 新索引表允许拆批产生的多个 OneBot 消息映射到同一组 Telegram 消息。
        CREATE TABLE IF NOT EXISTS onebot_message_mappings (
            mapping_id INTEGER NOT NULL REFERENCES message_mappings(id) ON DELETE CASCADE,
            q_group_id INTEGER NOT NULL,
            q_message_id INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY (q_group_id, q_message_id),
            UNIQUE (mapping_id, position)
        );

        -- 版本 0 的历史数据每条只有一个 OneBot ID，以 position=0 回填。
        -- OR IGNORE 使“表已由开发版创建但 user_version 仍为 0”的数据库也可升级。
        INSERT OR IGNORE INTO onebot_message_mappings (
            mapping_id, q_group_id, q_message_id, position
        )
        SELECT id, q_group_id, q_message_id, 0 FROM message_mappings;
    """,
}


async def migrate_database(db: aiosqlite.Connection) -> None:
    """读取 ``user_version``，并在独立事务中依次执行尚未运行的迁移。"""
    expected_versions = set(range(1, LATEST_DATABASE_VERSION + 1))
    if set(MIGRATIONS) != expected_versions:
        raise RuntimeError(
            "数据库迁移编号必须是从 1 到 "
            f"{LATEST_DATABASE_VERSION} 的连续整数"
        )

    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await cursor.close()
    current_version = int(row[0]) if row is not None else 0
    if current_version > LATEST_DATABASE_VERSION:
        raise RuntimeError(
            f"数据库版本 {current_version} 高于程序支持的版本 {LATEST_DATABASE_VERSION}"
        )

    for version in range(current_version + 1, LATEST_DATABASE_VERSION + 1):
        migration = MIGRATIONS[version]
        try:
            # executescript 会先提交已有事务，因此迁移脚本显式包裹自己的事务。
            # user_version 只在本版本 SQL 全部成功后更新并一同提交。
            await db.executescript(
                f"BEGIN IMMEDIATE;\n{migration}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except BaseException:
            await db.rollback()
            raise
