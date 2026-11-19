"""HTTP layer: turns requests into service calls, and errors into status codes."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__, instances, service
from app.cache import LinkCache
from app.config import DEFAULT_CODE_SECRET, settings
from app.db import engine, get_session
from app.ids import CodeCipher, LinkIds, RetiredError, Snowflake
from app.limits import LimitBodySize
from app.models import Link
from app.ratelimit import RateLimit
from app.redis_client import RedisGateway, get_redis
from app.schemas import LinkCreate, LinkOut

log = logging.getLogger(__name__)


def give_up(snowflake: Snowflake) -> None:
    """Our instance number may be someone else's now (app/instances.py). Make
    no more IDs, and shut down as `docker stop` would: Docker starts the
    instance again (restart: unless-stopped), and it leases a number afresh."""
    snowflake.retire()
    signal.raise_signal(signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The tables are not created here: migrations do that (`alembic upgrade
    # head`), once, before any instance starts. Three instances creating
    # tables at the same moment would race.
    secret = settings.code_secret.get_secret_value()
    if secret == DEFAULT_CODE_SECRET:
        log.warning("CODE_SECRET is not set: anyone can work out the codes. Set it in production.")
    # Hold a unique instance number for as long as this instance runs, and
    # keep checking that we still do.
    with instances.lease_instance_id(engine) as lease:
        snowflake = Snowflake(lease.number)
        app.state.ids = LinkIds(snowflake, CodeCipher(secret))
        watchdog = asyncio.create_task(instances.watch(lease, partial(give_up, snowflake)))
        try:
            yield
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog


app = FastAPI(title="URL Shortener", version=__version__, lifespan=lifespan)
# Refuse oversized request bodies before reading them (NASA/JPL rule 3).
app.add_middleware(LimitBodySize)
# Behind Nginx, every request comes from Nginx's address. The real client's
# address is in X-Forwarded-For, but anyone can write that header, so we read
# it only on requests from a proxy listed in TRUSTED_PROXIES. After this,
# request.client is the real client (the rate limiter counts per client).
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_list)


def get_cache(gateway: Annotated[RedisGateway | None, Depends(get_redis)]) -> LinkCache | None:
    if gateway is None:
        return None
    return LinkCache(gateway, settings.cache_ttl_seconds, settings.cache_negative_ttl_seconds)


def get_ids(request: Request) -> LinkIds:
    ids: LinkIds = request.app.state.ids  # made in lifespan(), at startup
    return ids


# "Annotated" dependencies: declared once, reused by every route that needs them.
SessionDep = Annotated[Session, Depends(get_session)]
CacheDep = Annotated[LinkCache | None, Depends(get_cache)]
IdsDep = Annotated[LinkIds, Depends(get_ids)]

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
def health(ids: IdsDep) -> dict[str, str | int]:
    # "instance" tells you which instance answered. Ask a few times through
    # Nginx and watch the load balancer take turns.
    return {"status": "ok", "instance": ids.snowflake.instance_id}


@app.post("/api/links", status_code=status.HTTP_201_CREATED, dependencies=[limit_create])
def create(body: LinkCreate, session: SessionDep, cache: CacheDep, ids: IdsDep) -> LinkOut:
    try:
        link = service.create_link(
            session, ids, str(body.url), body.alias, body.expires_in_days, cache=cache
        )
    except service.AliasTakenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alias is already taken") from exc
    except (service.IdClashError, RetiredError) as exc:
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
    # Reject impossible codes before touching the cache or the database (only
    # the rate limiter's script runs in Redis), so junk requests can't fill the
    # cache with "not found" entries.
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
