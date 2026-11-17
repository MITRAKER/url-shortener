"""Request/response shapes. Pydantic validates input before our code sees it."""
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl

ALIAS_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"


class LinkCreate(BaseModel):
    url: HttpUrl  # rejects anything that is not a valid http(s) URL
    alias: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: datetime | None
    clicks: int
