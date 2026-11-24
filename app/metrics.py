"""Prometheus metrics: what the app and the click worker count and time.

Prometheus *pulls*: every 15 seconds it asks each app instance for its
/metrics page, and the click worker for its own
(monitoring/prometheus/prometheus.yml). A page is plain text: counters,
gauges and histograms with their current values. A process keeps only
running totals since it started; Prometheus stores them over time and does
the arithmetic: rates, ratios, percentiles. /metrics is for Prometheus, on
the internal network: Nginx refuses it from outside.

Every label value comes from a short, fixed list (a route's template, a known
method, a status code), never straight from the request: each distinct value
is a new time series held in memory, and anyone can send `GET /aaaa`, `GET
/aaab`... or a method called `FOOBAR`.
"""

import logging
import time
from collections.abc import Iterable, Iterator
from typing import Final

import redis
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

# Re-exported for the /metrics route and the worker.
__all__ = ["CONTENT_TYPE_LATEST", "REGISTRY", "generate_latest"]

METRICS_PATH: Final = "/metrics"
# Requests that matched no route share one label: a 404 for /no/such/page,
# the redirect from /health/ to /health, which the router sends itself, and
# a request refused before routing: a 413 for a Content-Length over 16 KB
# (app/limits.py). A chunked body announces no size, so it is refused only
# once it passes 16 KB, inside its route: that 413 keeps the route's label.
UNMATCHED: Final = "unmatched"
KNOWN_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

# --- Each app instance: the default registry, served at /metrics ----------
# It also holds the process's own metrics: CPU time, memory, open files.

# The RED method for a request-driven service: Rate, Errors, Duration.
REQUESTS: Final = Counter(
    "http_requests_total",
    "HTTP requests answered, by method, route and status code.",
    ["method", "route", "status"],
)
# Buckets in seconds, finest where our redirects live (a few milliseconds).
# A histogram counts the requests that took up to each bucket's time;
# Prometheus estimates percentiles from the counts, across all instances at
# once. 0.1 is a bucket on purpose: it is our latency objective
# (monitoring/prometheus/alerts.yml), so "how many took 100 ms or less" is
# exact.
DURATION: Final = Histogram(
    "http_request_duration_seconds",
    "Time from receiving a request to sending the end of the answer.",
    ["method", "route"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
IN_PROGRESS: Final = Gauge("http_requests_in_progress", "HTTP requests being answered right now.")

CACHE_LOOKUPS: Final = Counter(
    "cache_lookups_total",
    "Redirect lookups, by where the answer came from: hit, miss or bypass (as in X-Cache).",
    ["result"],
)
CLICKS: Final = Counter(
    "clicks_recorded_total",
    "Clicks recorded, by path: the Redis stream, or the database when Redis couldn't take them.",
    ["path"],
)
RATE_LIMITED: Final = Counter(
    "rate_limited_requests_total", "Requests refused with 429, by limit.", ["limit"]
)
REDIS_FALLBACKS: Final = Counter(
    "redis_fallbacks_total",
    "Redis calls that fell back to doing without Redis, by operation and reason.",
    ["operation", "reason"],
)
# A gauge is only looked at every 15 seconds: a circuit that opens for 5
# seconds in between would never show. A counter can't miss it.
CIRCUIT_OPEN: Final = Gauge(
    "circuit_breaker_open", "1 while the circuit breaker skips the dependency, else 0.", ["name"]
)
CIRCUIT_OPENED: Final = Counter(
    "circuit_breaker_opened_total", "Times the circuit breaker opened.", ["name"]
)
APP: Final = Info("app", "The app's version, and this instance's number (see /health).")

# --- The click worker: a registry of its own ------------------------------
# The worker answers no HTTP requests, so the request metrics above have no
# place on its page. It serves this registry instead (app/worker.py).
WORKER_REGISTRY: Final = CollectorRegistry()
for _collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
    WORKER_REGISTRY.register(_collector)

BATCHES: Final = Counter(
    "click_worker_batches_total",
    "Batches read from the stream, by result: counted, or already counted by another worker.",
    ["result"],
    registry=WORKER_REGISTRY,
)
CLICKS_COUNTED: Final = Counter(
    "click_worker_clicks_counted_total",
    "Clicks added to the database.",
    registry=WORKER_REGISTRY,
)
ENTRIES_SKIPPED: Final = Counter(
    "click_worker_entries_skipped_total",
    "Stream entries skipped because they were not clicks on a valid code.",
    registry=WORKER_REGISTRY,
)
WORKER_ERRORS: Final = Counter(
    "click_worker_errors_total",
    "Rounds that failed and will be tried again, by the system that failed.",
    ["system"],
    registry=WORKER_REGISTRY,
)
BATCH_DURATION: Final = Histogram(
    "click_worker_batch_duration_seconds",
    "Time to count one batch: one transaction, then removing it from the stream.",
    buckets=(0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=WORKER_REGISTRY,
)

# Counters with labels appear only once a label value is first used. Create
# the known ones now, at 0, so that a query sees "0 so far", not "nothing".
for _result in ("hit", "miss", "bypass"):
    CACHE_LOOKUPS.labels(_result)
for _path in ("stream", "database"):
    CLICKS.labels(_path)
for _result in ("counted", "already_counted"):
    BATCHES.labels(_result)
for _system in ("redis", "database"):
    WORKER_ERRORS.labels(_system)


def method_label(method: str) -> str:
    return method if method in KNOWN_METHODS else "other"


class _StatusRecorder:
    """Wraps ASGI `send` to note the status code of the response."""

    def __init__(self, send: Send) -> None:
        self._send = send
        # If the app fails before it answers, Starlette answers 500.
        self.status = 500

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        await self._send(message)


class PrometheusMiddleware:
    """ASGI middleware: counts and times every HTTP request but /metrics.

    The route label is the route's template ("/{code}"), which FastAPI's
    router leaves in the request's scope once it has picked one of our
    routes. It doesn't for FastAPI's own pages (/docs,
    /docs/oauth2-redirect, /redoc, /openapi.json), which are routes of
    another kind: those are labelled with their paths, given as
    `docs_paths`.
    """

    def __init__(self, app: ASGIApp, docs_paths: Iterable[str | None] = ()) -> None:
        self.app = app
        # A fixed list, known at startup. None is a page switched off.
        self.docs_paths = frozenset(path for path in docs_paths if path is not None)

    def route_label(self, scope: Scope) -> str:
        route = getattr(scope.get("route"), "path", None)
        if route is not None:
            return str(route)
        path = str(scope["path"])
        return path if path in self.docs_paths else UNMATCHED

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == METRICS_PATH:
            await self.app(scope, receive, send)
            return
        recorder = _StatusRecorder(send)
        start = time.perf_counter()
        IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, recorder.send)
        finally:
            IN_PROGRESS.dec()
            method = method_label(scope["method"])
            route = self.route_label(scope)
            REQUESTS.labels(method, route, str(recorder.status)).inc()
            DURATION.labels(method, route).observe(time.perf_counter() - start)


class DatabasePoolCollector(Collector):
    """How busy the database connection pool is, measured when Prometheus
    asks. Requests wait for a connection once all are in use: the pool is
    saturated (the S of the USE method: Utilization, Saturation, Errors)."""

    def __init__(self, engine: Engine, limit: int) -> None:
        self._engine = engine
        self._limit = limit

    def collect(self) -> Iterator[Metric]:
        pool = self._engine.pool
        if not isinstance(pool, QueuePool):  # SQLite in memory, for example
            return
        yield GaugeMetricFamily(
            "db_pool_connections_in_use", "Database connections lent out.", pool.checkedout()
        )
        yield GaugeMetricFamily(
            "db_pool_connections_limit", "The most connections the pool lends.", self._limit
        )


class BacklogCollector(Collector):
    """The clicks waiting in the stream, measured when Prometheus asks: how
    many, and how long ago the oldest arrived. The worker removes the clicks
    it has counted (app/worker.py), so whatever is in the stream is waiting.
    The worker serves these, and so does every app instance (app/main.py)."""

    def __init__(self, client: redis.Redis, stream: str) -> None:
        self._client = client
        self._stream = stream

    def describe(self) -> Iterator[Metric]:
        """The metrics, without values. The app's registry asks for them when
        the collector is registered, to check their names; without this, it
        would collect them, and wait for Redis."""
        return self._metrics(None, None)

    def collect(self) -> Iterator[Metric]:
        try:
            # One round trip for three commands. TIME is Redis's clock, the
            # one that stamped the entry IDs.
            pipe = self._client.pipeline(transaction=False)
            pipe.xlen(self._stream)
            pipe.xrange(self._stream, count=1)
            pipe.time()
            length, first, (seconds, micros) = pipe.execute()
        except redis.RedisError as exc:
            # No numbers is better than wrong ones: the page goes out without them.
            log.warning("could not measure the clicks waiting in the stream: %s", exc)
            return
        now_ms = seconds * 1000 + micros // 1000
        # An entry ID starts with the time it was added, in milliseconds.
        oldest_ms = int(first[0][0].partition("-")[0]) if first else now_ms
        yield from self._metrics(length, max(0, now_ms - oldest_ms) / 1000)

    @staticmethod
    def _metrics(length: float | None, age: float | None) -> Iterator[Metric]:
        yield GaugeMetricFamily(
            "click_backlog_entries", "Clicks waiting in the stream to be counted.", length
        )
        yield GaugeMetricFamily(
            "click_backlog_oldest_age_seconds",
            "How long the oldest click waiting in the stream has waited (0 when none waits).",
            age,
        )
