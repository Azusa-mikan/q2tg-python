"""增加应用级持久化配置表。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_application_settings"
down_revision: str | Sequence[str] | None = "0003_mapping_id_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("application_settings")
