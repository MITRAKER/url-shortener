"""Database tables."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# BIGINT in Postgres; plain INTEGER in SQLite, where only INTEGER PRIMARY KEY
# columns auto-increment.
BigInt = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    # The unique index on `code` is what makes redirects fast (O(log n) lookup)
    # and what guarantees no two links share a code.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clicks: Mapped[int] = mapped_column(BigInt, default=0)
