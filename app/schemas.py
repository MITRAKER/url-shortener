"""Request/response shapes. Pydantic validates input before our code sees it."""

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, HttpUrl, UrlConstraints
from pydantic_core import PydanticCustomError

ALIAS_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
# Every input has a maximum size (NASA/JPL rule 3: bounded resources).
# 2048 characters is the common practical limit for URLs.
MAX_URL_LENGTH = 2048


def _at_most_max_length(url: HttpUrl) -> HttpUrl:
    """Measure the URL as we store it and redirect to it.

    UrlConstraints counts the UTF-8 bytes of the input as typed. Pydantic then
    normalizes the URL, which percent-encodes non-ASCII characters in the path
    ("é" becomes "%C3%A9"), so on its own it would accept a 2048-byte URL made
    mostly of emoji and store it as more than 6000 characters.
    """
    if len(str(url)) > MAX_URL_LENGTH:
        # The error type and message of UrlConstraints, so clients see one kind of error.
        raise PydanticCustomError(
            "url_too_long",
            "URL should have at most {max_length} characters",
            {"max_length": MAX_URL_LENGTH},
        )
    return url


class LinkCreate(BaseModel):
    # Rejects anything that is not a valid http(s) URL of at most 2048 characters
    # as stored. UrlConstraints is the cheap first check, on the input before it
    # is parsed; the AfterValidator measures the normalized URL we store.
    url: Annotated[
        HttpUrl, UrlConstraints(max_length=MAX_URL_LENGTH), AfterValidator(_at_most_max_length)
    ]
    alias: str | None = Field(default=None, pattern=ALIAS_PATTERN)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: datetime | None
    clicks: int
