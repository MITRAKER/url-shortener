"""Request/response shapes. Pydantic validates input before our code sees it."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, UrlConstraints

ALIAS_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
# Every input has a maximum size (NASA/JPL rule 3: bounded resources).
# 2048 characters is the common practical limit for URLs.
MAX_URL_LENGTH = 2048


class LinkCreate(BaseModel):
    # Rejects anything that is not a valid http(s) URL of at most 2048 characters.
    url: Annotated[HttpUrl, UrlConstraints(max_length=MAX_URL_LENGTH)]
    alias: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: datetime | None
    clicks: int
