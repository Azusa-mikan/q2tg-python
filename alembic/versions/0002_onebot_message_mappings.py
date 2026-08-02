"""增加一条逻辑映射对应多个 OneBot 消息的索引。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.database_schema import mapping_id_type

revision: str = "0002_onebot_message_mappings"
down_revision: str | Sequence[str] | None = "0001_legacy_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onebot_message_mappings",
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
    op.execute(
        """
        INSERT INTO onebot_message_mappings (
            mapping_id, q_group_id, q_message_id, position
        )
        SELECT id, q_group_id, q_message_id, 0 FROM message_mappings
        """
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute("PRAGMA user_version = 1")


def downgrade() -> None:
    op.drop_table("onebot_message_mappings")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("PRAGMA user_version = 0")
