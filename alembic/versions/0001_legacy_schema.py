"""创建历史基础 schema。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.database_schema import mapping_id_type

revision: str = "0001_legacy_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_mappings",
        sa.Column(
            "q_group_id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("tg_forward_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id_show_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "message_mappings",
        sa.Column("id", mapping_id_type, primary_key=True, autoincrement=True),
        sa.Column("q_group_id", sa.BigInteger(), nullable=False),
        sa.Column("q_message_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("q_user_id", sa.BigInteger()),
        sa.Column("tg_user_id", sa.BigInteger()),
        sa.Column("expires_at", sa.Double(), nullable=False),
        sa.UniqueConstraint("q_group_id", "q_message_id"),
    )
    op.create_table(
        "telegram_message_mappings",
        sa.Column(
            "mapping_id",
            mapping_id_type,
            sa.ForeignKey("message_mappings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("tg_chat_id", "tg_message_id"),
    )
    op.create_index(
        "message_mappings_expires_at",
        "message_mappings",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("message_mappings_expires_at", table_name="message_mappings")
    op.drop_table("telegram_message_mappings")
    op.drop_table("message_mappings")
    op.drop_table("group_mappings")
