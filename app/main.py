"""HTTP layer: turns requests into service calls, and errors into status codes."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Annotated, Final

from anyio import CapacityLimiter, to_thread
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__, instances, metrics, service
from app.cache import LinkCache
from app.clicks import STREAM, ClickStream
from app.config import DEFAULT_CODE_SECRET, settings
from app.db import POOL_MAX_OVERFLOW, POOL_SIZE, engine, get_session
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


@contextmanager
def backlog_reported(gateway: RedisGateway | None) -> Iterator[None]:
    """Show the clicks waiting in the stream on this instance's /metrics page
    while the `with` block runs. The worker shows them too, but its page goes
    when it stops, and that is when the clicks pile up: from the instances'
    pages, ClicksWaitingTooLong still sees them (monitoring/prometheus)."""
    if gateway is None:
        yield  # no Redis, no stream
        return
    collector = metrics.BacklogCollector(gateway.client, STREAM)
    metrics.REGISTRY.register(collector)
    try:
        yield
    finally:
        metrics.REGISTRY.unregister(collector)


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
    with instances.lease_instance_id(engine) as lease, backlog_reported(get_redis()):
        metrics.APP.info({"version": __version__, "instance_number": str(lease.number)})
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
# Count and time every request (app/metrics.py). Added last, so it runs
# first: it also sees the requests that the middleware above refuses. It is
# given the paths of FastAPI's own pages (the API docs), to label them.
app.add_middleware(
    metrics.PrometheusMiddleware,
    docs_paths=[app.openapi_url, app.docs_url, app.swagger_ui_oauth2_redirect_url, app.redoc_url],
)
# How busy the connection pool is. PostgreSQL only: that is the server whose
# connections run out (SQLite's pool, a file on this machine, sizes itself).
if engine.dialect.name == "postgresql":
    metrics.REGISTRY.register(metrics.DatabasePoolCollector(engine, POOL_SIZE + POOL_MAX_OVERFLOW))


def get_cache(gateway: Annotated[RedisGateway | None, Depends(get_redis)]) -> LinkCache | None:
    if gateway is None:
        return None
    return LinkCache(gateway, settings.cache_ttl_seconds, settings.cache_negative_ttl_seconds)


def get_clicks(gateway: Annotated[RedisGateway | None, Depends(get_redis)]) -> ClickStream | None:
    return None if gateway is None else ClickStream(gateway)


def get_ids(request: Request) -> LinkIds:
    ids: LinkIds = request.app.state.ids  # made in lifespan(), at startup
    return ids


# "Annotated" dependencies: declared once, reused by every route that needs them.
SessionDep = Annotated[Session, Depends(get_session)]
CacheDep = Annotated[LinkCache | None, Depends(get_cache)]
ClicksDep = Annotated[ClickStream | None, Depends(get_clicks)]
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


# One scrape at a time, on a thread that isn't one of the requests' (below).
SCRAPE_LIMITER: Final = CapacityLimiter(1)


# For Prometheus (monitoring/prometheus/prometheus.yml), which asks every
# instance directly; Nginx refuses it from outside. Not rate limited. The page
# is put together on a thread of its own, for two reasons. Measuring the click
# backlog asks Redis, which may take up to its timeout: on the event loop,
# that wait would hold up every request. And the thread runs under a limiter
# of its own, not under the requests' limit of 40 threads: an overloaded
# instance, every thread busy, must still report.
@app.get(metrics.METRICS_PATH, include_in_schema=False)
async def prometheus_metrics() -> Response:
    page = await to_thread.run_sync(metrics.generate_latest, limiter=SCRAPE_LIMITER)
    return Response(page, media_type=metrics.CONTENT_TYPE_LATEST)


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
    # Not cached on purpose: the click count keeps changing. It may lag a
    # second or two behind the clicks, while the click worker catches up.
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return to_out(link)


@app.head("/{code}", dependencies=[limit_read], include_in_schema=False)
@app.get("/{code}", dependencies=[limit_read])
def redirect(
    code: str, request: Request, session: SessionDep, cache: CacheDep, clicks: ClicksDep
) -> RedirectResponse:
    # Reject impossible codes before touching Redis or Postgres, so junk
    # requests can't fill the cache with "not found" entries.
    if not service.CODE_PATTERN.fullmatch(code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")

    link, cache_status = service.resolve(session, cache, code)
    metrics.CACHE_LOOKUPS.labels(cache_status.value.lower()).inc()
    headers = {"X-Cache": cache_status.value}
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found", headers=headers)
    if service.is_expired(link):
        raise HTTPException(status.HTTP_410_GONE, "Link has expired", headers=headers)
    # HEAD asks "where does this go?" without going there: a link checker or a
    # chat app's preview, not a person clicking. Only GET counts as a click.
    # The click goes to a Redis stream: on a cache hit, a redirect doesn't
    # touch PostgreSQL at all (app/clicks.py).
    if request.method == "GET":
        service.count_click(session, clicks, code)
    # 302 (temporary) instead of 301 (permanent): browsers cache 301s forever,
    # so later clicks would never reach us and could not be counted.
    return RedirectResponse(link.target_url, status_code=status.HTTP_302_FOUND, headers=headers)
