"""增加 Telegram 映射外键索引。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_mapping_id_index"
down_revision: str | Sequence[str] | None = "0002_onebot_message_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "telegram_message_mappings_mapping_id",
        "telegram_message_mappings",
        ["mapping_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "telegram_message_mappings_mapping_id",
        table_name="telegram_message_mappings",
    )
