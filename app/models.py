"""Database tables."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# BIGINT in Postgres; INTEGER in SQLite, where INTEGER already holds 64 bits
# and an INTEGER PRIMARY KEY is the table's own row ID, the fastest key.
BigInt = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Link(Base):
    __tablename__ = "links"

    # A Snowflake ID (app/ids.py), made by the app before the INSERT, not
    # counted up by the database: no instance has to ask anyone for the next
    # number, and IDs still sort by creation time.
    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=False)
    # The unique index on `code` is what makes redirects fast (O(log n) lookup)
    # and what guarantees no two links share a code.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicks: Mapped[int] = mapped_column(BigInt, default=0)


class ClickCheckpoint(Base):
    """How far the click worker got in a Redis stream (app/worker.py)."""

    __tablename__ = "click_checkpoints"

    stream: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The ID of the last stream entry whose click is already in links.clicks.
    # It changes in the same transaction as the counts, so the two always agree.
    last_id: Mapped[str] = mapped_column(String(64))
