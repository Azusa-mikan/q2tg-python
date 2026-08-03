"""增加其他 Telegram Bot 消息转发开关。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_bot_forward"
down_revision: str | Sequence[str] | None = "0004_application_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "group_mappings",
        sa.Column(
            "bot_forward_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("group_mappings", "bot_forward_enabled")
