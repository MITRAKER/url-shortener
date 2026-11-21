"""Link IDs come from the app (Snowflake IDs), not from a database counter.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    # In SQLite an INTEGER PRIMARY KEY simply takes the ID it is given, so
    # there is nothing to change there.
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    # A BIGSERIAL column is a sequence plus a default that takes the next
    # number from it. The app now chooses every ID, so both go. (IF EXISTS:
    # a table made by a newer app version never had the sequence.)
    op.alter_column("links", "id", server_default=None)
    op.execute("DROP SEQUENCE IF EXISTS links_id_seq")


def downgrade() -> None:
    if not _is_postgres():
        return
    # Back to a counter, starting above the largest ID in use.
    op.execute("CREATE SEQUENCE links_id_seq OWNED BY links.id")
    op.execute("SELECT setval('links_id_seq', COALESCE((SELECT max(id) FROM links), 0) + 1, false)")
    op.alter_column("links", "id", server_default=sa.text("nextval('links_id_seq'::regclass)"))
