"""The links table as it was in v0.2.0.

Revision ID: 0001
Revises:
Create Date: 2026-11-18

Before migrations, every app instance created this table itself when it
started (steps 1 and 2). A database from those days already has it: this
migration then adopts the table as it is, and the next ones carry on from
there. That is called a baseline.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BIGINT in PostgreSQL, INTEGER in SQLite (see app/models.py).
BigInt = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _has_links_table() -> bool:
    if op.get_context().as_sql:  # `--sql` prints SQL; there is no database to look at
        return False
    return sa.inspect(op.get_bind()).has_table("links")


def upgrade() -> None:
    if _has_links_table():
        return
    op.create_table(
        "links",
        # autoincrement: in PostgreSQL a BIGSERIAL, a column that takes its
        # values from a counter (a sequence). Revision 0002 removes it.
        sa.Column("id", BigInt, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicks", BigInt, nullable=False),
    )
    op.create_index("ix_links_code", "links", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_links_code", table_name="links")
    op.drop_table("links")
