"""Where the click worker got to in the click stream (app/worker.py).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table, nothing else: instances still running the previous version
    # never notice it (expand first, see CONTRIBUTING.md).
    op.create_table(
        "click_checkpoints",
        sa.Column("stream", sa.String(64), primary_key=True),
        sa.Column("last_id", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("click_checkpoints")
