"""SQLAlchemy 业务表定义，供数据访问层与 Alembic 共用。"""

import sqlalchemy as sa

metadata = sa.MetaData()

# SQLite 只有精确的 INTEGER PRIMARY KEY 才会自动生成 rowid。
mapping_id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

application_settings = sa.Table(
    "application_settings",
    metadata,
    sa.Column("key", sa.String(64), primary_key=True),
    sa.Column("value", sa.Text(), nullable=False),
)

group_mappings = sa.Table(
    "group_mappings",
    metadata,
    sa.Column("q_group_id", sa.BigInteger(), primary_key=True, autoincrement=False),
    sa.Column("tg_chat_id", sa.BigInteger(), nullable=False, unique=True),
    sa.Column(
        "tg_forward_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    ),
    sa.Column(
        "bot_forward_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "id_show_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    ),
)

message_mappings = sa.Table(
    "message_mappings",
    metadata,
    sa.Column("id", mapping_id_type, primary_key=True, autoincrement=True),
    sa.Column("q_group_id", sa.BigInteger(), nullable=False),
    sa.Column("q_message_id", sa.BigInteger(), nullable=False),
    sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
    sa.Column("q_user_id", sa.BigInteger()),
    sa.Column("tg_user_id", sa.BigInteger()),
    sa.Column("expires_at", sa.Double(), nullable=False),
    sa.UniqueConstraint("q_group_id", "q_message_id"),
    sa.Index("message_mappings_expires_at", "expires_at"),
)

telegram_message_mappings = sa.Table(
    "telegram_message_mappings",
    metadata,
    sa.Column(
        "mapping_id",
        mapping_id_type,
        sa.ForeignKey("message_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
    sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint("tg_chat_id", "tg_message_id"),
    sa.Index("telegram_message_mappings_mapping_id", "mapping_id"),
)

onebot_message_mappings = sa.Table(
    "onebot_message_mappings",
    metadata,
    sa.Column(
        "mapping_id",
        mapping_id_type,
        sa.ForeignKey("message_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("q_group_id", sa.BigInteger(), nullable=False),
    sa.Column("q_message_id", sa.BigInteger(), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.CheckConstraint("position >= 0"),
    sa.PrimaryKeyConstraint("q_group_id", "q_message_id"),
    sa.UniqueConstraint("mapping_id", "position"),
)
