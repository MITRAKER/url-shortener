"""HTTP layer: turns requests into service calls, and errors into status codes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import __version__, service
from app.cache import LinkCache
from app.config import settings
from app.db import Base, engine, get_session
from app.limits import LimitBodySize
from app.models import Link
from app.ratelimit import RateLimit
from app.redis_client import RedisGateway, get_redis
from app.schemas import LinkCreate, LinkOut


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Creates the tables if they are missing. Fine for one instance, but it
    # checks first and creates second, so several instances starting at the same
    # moment on an empty database would race. Production apps run migrations
    # (e.g. Alembic) once, before any instance starts.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="URL Shortener", version=__version__, lifespan=lifespan)
# Refuse oversized request bodies before reading them (NASA/JPL rule 3).
app.add_middleware(LimitBodySize)


def get_cache(gateway: Annotated[RedisGateway | None, Depends(get_redis)]) -> LinkCache | None:
    if gateway is None:
        return None
    return LinkCache(gateway, settings.cache_ttl_seconds, settings.cache_negative_ttl_seconds)


# "Annotated" dependencies: declared once, reused by every route that needs them.
SessionDep = Annotated[Session, Depends(get_session)]
CacheDep = Annotated[LinkCache | None, Depends(get_cache)]

# Creating links is the expensive, abusable operation, so it gets a strict
# limit. Reads (redirects and stats) share a generous one.
limit_create = Depends(RateLimit("create", settings.rate_limit_create))
limit_read = Depends(RateLimit("read", settings.rate_limit_read))


def to_out(link: Link) -> LinkOut:
    return LinkOut(
        code=link.code,
        short_url=f"{settings.base_url.rstrip('/')}/{link.code}",
        target_url=link.target_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        clicks=link.clicks,
    )


# HEAD is GET without the body. Link checkers and uptime monitors use it, so
# every page should answer it. (Hidden from the API docs to avoid duplicates.)
@app.head("/health", include_in_schema=False)
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/links", status_code=status.HTTP_201_CREATED, dependencies=[limit_create])
def create(body: LinkCreate, session: SessionDep, cache: CacheDep) -> LinkOut:
    try:
        link = service.create_link(
            session, str(body.url), body.alias, body.expires_in_days, cache=cache
        )
    except service.AliasTakenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alias is already taken") from exc
    except service.CodeSpaceExhaustedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not allocate a code"
        ) from exc
    return to_out(link)


@app.get("/api/links/{code}", dependencies=[limit_read])
def stats(code: str, session: SessionDep) -> LinkOut:
    # Not cached on purpose: the click count changes on every redirect.
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return to_out(link)


@app.head("/{code}", dependencies=[limit_read], include_in_schema=False)
@app.get("/{code}", dependencies=[limit_read])
def redirect(code: str, request: Request, session: SessionDep, cache: CacheDep) -> RedirectResponse:
    # Reject impossible codes before touching Redis or Postgres, so junk
    # requests can't fill the cache with "not found" entries.
    if not service.CODE_PATTERN.fullmatch(code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")

    link, cache_status = service.resolve(session, cache, code)
    headers = {"X-Cache": cache_status.value}
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found", headers=headers)
    if service.is_expired(link):
        raise HTTPException(status.HTTP_410_GONE, "Link has expired", headers=headers)
    # HEAD asks "where does this go?" without going there: a link checker or a
    # chat app's preview, not a person clicking. Only GET counts as a click.
    # (Still a database write on every click, even on a cache hit; step 4
    # moves click counting off this path.)
    if request.method == "GET":
        service.record_click(session, code)
    # 302 (temporary) instead of 301 (permanent): browsers cache 301s forever,
    # so later clicks would never reach us and could not be counted.
    return RedirectResponse(link.target_url, status_code=status.HTTP_302_FOUND, headers=headers)
