"""Metrics for Prometheus (app/metrics.py): what each instance counts, and how.

Metrics live in one registry per process, shared by every test, so each test
reads a metric before and after, and checks the difference.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
import redis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from prometheus_client.core import Metric
from sqlalchemy import create_engine
from starlette.types import Message, Receive, Scope, Send

from app import __version__
from app import main as main_module
from app.breaker import CircuitBreaker
from app.clicks import STREAM
from app.limits import MAX_BODY_BYTES
from app.metrics import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    BacklogCollector,
    DatabasePoolCollector,
    PrometheusMiddleware,
)
from app.redis_client import RedisGateway, make_redis
from tests.fakes import FakeClock, SilentServer

URL = "https://example.com/"


def value(metric: str, /, **labels: str) -> float:
    """A metric's current value in the app's registry (0 if never set)."""
    return REGISTRY.get_sample_value(metric, labels) or 0.0


def requests(method: str, route: str, status: int) -> float:
    return value("http_requests_total", method=method, route=route, status=str(status))


def new_link(client: TestClient) -> str:
    response = client.post("/api/links", json={"url": URL})
    assert response.status_code == 201
    return str(response.json()["code"])


async def receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def ignore(_message: Message) -> None:
    """An ASGI `send` that drops what it is given."""


def call(middleware: PrometheusMiddleware, path: str = "/x") -> None:
    scope: Scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    asyncio.run(middleware(scope, receive, ignore))


# ------------------------------------------------------- the /metrics page ----


def test_the_metrics_page_is_for_prometheus(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "# TYPE http_requests_total counter" in response.text
    assert "# TYPE http_request_duration_seconds histogram" in response.text
    assert "process_cpu_seconds_total" in response.text  # the process's own metrics


def test_the_metrics_page_does_not_count_itself(client: TestClient) -> None:
    for _ in range(3):
        client.get("/metrics")
    assert requests("GET", "/metrics", 200) == 0


def test_the_metrics_page_is_not_in_the_api_docs(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_metrics_cannot_be_taken_as_an_alias(client: TestClient) -> None:
    response = client.post("/api/links", json={"url": URL, "alias": "metrics"})
    assert response.status_code == 409


@pytest.mark.usefixtures("client")  # the app has started, and leased its number
def test_the_version_and_instance_number_are_published() -> None:
    # The instance number tells which /health answer goes with which target
    # in Prometheus (on SQLite there is only instance 0).
    assert value("app_info", version=__version__, instance_number="0") == 1


@contextmanager
def instance(gateway: RedisGateway, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A started app instance whose own Redis is `gateway`: the one it gets
    from get_redis() at startup, to measure the backlog. (The `client`
    fixtures set only the Redis that requests get.)"""
    monkeypatch.setattr(main_module, "get_redis", lambda: gateway)
    with TestClient(main_module.app) as client:
        yield client


def test_the_metrics_page_shows_the_clicks_waiting(
    redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker shows them too, but its page goes when it stops, and that
    is when the clicks pile up. So every instance shows them as well."""
    redis_client.xadd(STREAM, {"code": "first"})
    redis_client.xadd(STREAM, {"code": "second"})
    with instance(RedisGateway(redis_client), monkeypatch) as client:
        page = client.get("/metrics").text
    assert "click_backlog_entries 2.0" in page
    assert "click_backlog_oldest_age_seconds " in page


@pytest.mark.parametrize("hung", [False, True], ids=["refused", "hung"])
def test_with_redis_down_the_metrics_page_comes_quickly_without_the_backlog(
    hung: bool,
    silent_server: SilentServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redis refuses at once, or never answers and the call gives up after
    its 0.25 s timeout. Either way the rest of the page comes, well within
    Prometheus's 10 s, and without the backlog: no number beats a wrong one."""
    gateway = RedisGateway(make_redis(silent_server.url if hung else "redis://127.0.0.1:1/0"))
    with instance(gateway, monkeypatch) as client:
        start = time.perf_counter()
        response = client.get("/metrics")
        elapsed = time.perf_counter() - start
    gateway.client.close()
    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "click_backlog_entries" not in response.text
    assert "could not measure the clicks waiting" in caplog.text
    assert elapsed < 1


class SlowCollector:
    """Stands for a Redis that takes its time: collects only when let go."""

    def __init__(self) -> None:
        self.waiting = threading.Event()
        self.go = threading.Event()
        self.request_threads_taken: int | None = None

    def describe(self) -> list[Metric]:
        return []  # so that registering it doesn't collect

    def collect(self) -> list[Metric]:
        # Asked of the event loop: how many of the requests' threads are taken?
        self.request_threads_taken = anyio.from_thread.run_sync(
            lambda: anyio.to_thread.current_default_thread_limiter().borrowed_tokens
        )
        self.waiting.set()
        self.go.wait(timeout=5)
        return []


def test_a_slow_scrape_holds_up_no_request(client: TestClient) -> None:
    """The page is put together on a thread of its own: requests are answered
    meanwhile, and the scrape takes none of the threads they share."""
    slow = SlowCollector()
    REGISTRY.register(slow)
    try:
        with ThreadPoolExecutor(1) as pool:
            scrape = pool.submit(client.get, "/metrics")
            assert slow.waiting.wait(timeout=5)
            assert client.get("/health").status_code == 200  # while the scrape waits
            slow.go.set()
            assert scrape.result(timeout=5).status_code == 200
    finally:
        slow.go.set()
        REGISTRY.unregister(slow)
    assert slow.request_threads_taken == 0


# ------------------------------------------------------------ requests ----


def test_requests_are_counted_by_route_template_not_by_path(client: TestClient) -> None:
    code = new_link(client)
    before = requests("GET", "/{code}", 302), requests("GET", "/api/links/{code}", 200)
    timed = value("http_request_duration_seconds_count", method="GET", route="/{code}")
    client.get(f"/{code}")
    client.get(f"/{code}")
    client.get(f"/api/links/{code}")
    after = requests("GET", "/{code}", 302), requests("GET", "/api/links/{code}", 200)
    assert after == (before[0] + 2, before[1] + 1)
    assert value("http_request_duration_seconds_count", method="GET", route="/{code}") == timed + 2


def test_status_codes_are_counted_as_answered(client: TestClient) -> None:
    before = requests("GET", "/{code}", 404), requests("POST", "/api/links", 422)
    client.get("/nosuchcode")
    client.post("/api/links", json={"url": "not a url"})
    after = requests("GET", "/{code}", 404), requests("POST", "/api/links", 422)
    assert after == (before[0] + 1, before[1] + 1)


def test_paths_that_match_no_route_share_one_label(client: TestClient) -> None:
    before = requests("GET", "unmatched", 404)
    for n in range(3):
        assert client.get(f"/no/such/page/{n}").status_code == 404
    assert requests("GET", "unmatched", 404) == before + 3


def test_the_api_docs_pages_are_counted_by_their_paths(client: TestClient) -> None:
    """FastAPI's own pages are routes too, but its router doesn't leave them
    in the scope as it does ours: the middleware is given their paths."""
    pages = ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"]
    before = [requests("GET", page, 200) for page in pages]
    for page in pages:
        assert client.get(page).status_code == 200
    assert [requests("GET", page, 200) for page in pages] == [n + 1 for n in before]


def test_a_redirect_that_drops_a_trailing_slash_is_unmatched(client: TestClient) -> None:
    """/health/ and /docs/ match no route; the router answers them with a
    redirect to the path without the slash."""
    before = requests("GET", "unmatched", 307)
    assert client.get("/health/").status_code == 307
    assert client.get("/docs/").status_code == 307
    assert requests("GET", "unmatched", 307) == before + 2


@pytest.mark.parametrize(
    ("chunked", "route"),
    [(False, "unmatched"), (True, "/api/links")],
    ids=["content-length", "chunked"],
)
def test_a_body_too_large_is_counted_where_it_is_refused(
    client: TestClient, chunked: bool, route: str
) -> None:
    """A Content-Length over 16 KB is refused before routing. A chunked body
    announces no size, so it is refused once it passes 16 KB, inside its route."""
    parts = [b'{"url": "https://example.com/', b"a" * MAX_BODY_BYTES, b'"}']
    body = iter(parts) if chunked else b"".join(parts)
    before = requests("POST", route, 413)
    response = client.post("/api/links", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert ("content-length" in response.request.headers) is not chunked
    assert requests("POST", route, 413) == before + 1


def test_unknown_methods_share_one_label(client: TestClient) -> None:
    before = requests("other", "/health", 405)
    assert client.request("FOOBAR", "/health").status_code == 405
    assert requests("other", "/health", 405) == before + 1


def test_a_request_that_crashes_counts_as_a_500() -> None:
    async def crash(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise RuntimeError("a bug")

    before = requests("GET", "unmatched", 500), value("http_requests_in_progress")
    with pytest.raises(RuntimeError, match="a bug"):
        call(PrometheusMiddleware(crash))
    assert requests("GET", "unmatched", 500) == before[0] + 1
    assert value("http_requests_in_progress") == before[1]


def test_requests_in_progress_are_counted_while_they_run() -> None:
    seen: list[float] = []

    async def answer(_scope: Scope, _receive: Receive, send: Send) -> None:
        seen.append(value("http_requests_in_progress"))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    before = value("http_requests_in_progress"), requests("GET", "unmatched", 204)
    call(PrometheusMiddleware(answer))
    assert seen == [before[0] + 1]
    assert value("http_requests_in_progress") == before[0]
    assert requests("GET", "unmatched", 204) == before[1] + 1


# ------------------------------------------------- what requests did ----


def test_cache_lookups_are_counted_by_result(client: TestClient) -> None:
    code = new_link(client)  # cached when created (write-through)
    before = value("cache_lookups_total", result="hit"), value("cache_lookups_total", result="miss")
    client.get(f"/{code}")  # hit
    client.get("/unknown")  # miss, then remembered as "not found"
    client.get("/unknown")  # hit
    after = value("cache_lookups_total", result="hit"), value("cache_lookups_total", result="miss")
    assert after == (before[0] + 2, before[1] + 1)


def test_lookups_without_redis_are_bypasses(client_without_redis: TestClient) -> None:
    code = new_link(client_without_redis)
    before = value("cache_lookups_total", result="bypass")
    client_without_redis.get(f"/{code}")
    assert value("cache_lookups_total", result="bypass") == before + 1


def test_clicks_go_to_the_stream(client: TestClient) -> None:
    code = new_link(client)
    before = value("clicks_recorded_total", path="stream")
    client.get(f"/{code}")
    client.head(f"/{code}")  # not a click
    assert value("clicks_recorded_total", path="stream") == before + 1


def test_without_redis_clicks_go_to_the_database(client_without_redis: TestClient) -> None:
    code = new_link(client_without_redis)
    before = value("clicks_recorded_total", path="database")
    client_without_redis.get(f"/{code}")
    assert value("clicks_recorded_total", path="database") == before + 1


def test_refused_requests_are_counted_by_limit(client: TestClient) -> None:
    before = value("rate_limited_requests_total", limit="create")
    statuses = [client.post("/api/links", json={"url": URL}).status_code for _ in range(11)]
    assert statuses.count(429) == 1
    assert value("rate_limited_requests_total", limit="create") == before + 1


def test_calls_that_do_without_redis_are_counted(client_with_redis_down: TestClient) -> None:
    def fallbacks(reason: str) -> float:
        return value("redis_fallbacks_total", operation="rate limiter", reason=reason)

    before = fallbacks("error"), fallbacks("circuit_open")
    # The rate limiter is the first call of each request. Three failures in
    # a row open the circuit (app/redis_client.py); then calls are skipped.
    for _ in range(4):
        assert client_with_redis_down.get("/api/links/nosuchcode").status_code == 404
    assert fallbacks("error") == before[0] + 3
    assert fallbacks("circuit_open") == before[1] + 1


def test_the_circuit_breaker_reports_its_state() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("metrics-test", failure_threshold=2, reset_after=5, clock=clock)
    assert value("circuit_breaker_open", name="metrics-test") == 0
    before = value("circuit_breaker_opened_total", name="metrics-test")
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()  # already open: not opened again
    assert value("circuit_breaker_open", name="metrics-test") == 1
    assert value("circuit_breaker_opened_total", name="metrics-test") == before + 1
    breaker.record_success()
    assert value("circuit_breaker_open", name="metrics-test") == 0


def test_the_circuit_reads_closed_once_the_cool_down_is_over() -> None:
    """After the cool-down, the breaker no longer skips Redis: it lets the
    next call through. An instance that gets no request makes no call, and
    must not report the circuit open for as long as it stays idle."""
    clock = FakeClock()
    breaker = CircuitBreaker("idle-test", failure_threshold=1, reset_after=5, clock=clock)
    breaker.record_failure()
    clock.advance(4.9)
    assert value("circuit_breaker_open", name="idle-test") == 1
    clock.advance(0.1)
    assert value("circuit_breaker_open", name="idle-test") == 0  # without a single call


# ------------------------------------------------------------ collectors ----


def test_the_pool_collector_counts_connections_in_use(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")  # a file: QueuePool
    registry = CollectorRegistry()
    registry.register(DatabasePoolCollector(engine, limit=30))
    try:
        with engine.connect():
            assert registry.get_sample_value("db_pool_connections_in_use") == 1
        assert registry.get_sample_value("db_pool_connections_in_use") == 0
        assert registry.get_sample_value("db_pool_connections_limit") == 30
    finally:
        engine.dispose()


def test_the_pool_collector_skips_pools_without_a_size() -> None:
    engine = create_engine("sqlite://")  # in memory: one connection per thread
    registry = CollectorRegistry()
    registry.register(DatabasePoolCollector(engine, limit=30))
    assert registry.get_sample_value("db_pool_connections_in_use") is None


def backlog(client: redis.Redis) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(BacklogCollector(client, STREAM))
    return registry


def test_an_empty_stream_is_no_backlog(redis_client: redis.Redis) -> None:
    registry = backlog(redis_client)
    assert registry.get_sample_value("click_backlog_entries") == 0
    assert registry.get_sample_value("click_backlog_oldest_age_seconds") == 0


def test_the_backlog_is_how_many_clicks_wait_and_for_how_long(redis_client: redis.Redis) -> None:
    seconds, micros = redis_client.time()
    five_seconds_ago = seconds * 1000 + micros // 1000 - 5000
    redis_client.xadd(STREAM, {"code": "first"}, id=f"{five_seconds_ago}-0")
    redis_client.xadd(STREAM, {"code": "second"})
    registry = backlog(redis_client)
    assert registry.get_sample_value("click_backlog_entries") == 2
    age = registry.get_sample_value("click_backlog_oldest_age_seconds")
    assert age is not None
    assert 5 <= age < 10


def test_without_redis_the_backlog_is_left_out(caplog: pytest.LogCaptureFixture) -> None:
    client = make_redis("redis://127.0.0.1:1/0")  # nothing listens there
    with caplog.at_level(logging.WARNING):
        registry = backlog(client)
        assert registry.get_sample_value("click_backlog_entries") is None
    assert "could not measure the clicks waiting" in caplog.text


def test_the_backlog_collector_is_registered_without_asking_redis(
    silent_server: SilentServer,
) -> None:
    """The app's registry checks the names of a collector as it registers it.
    The collector names its metrics itself: otherwise the registry would
    collect them, and an instance would wait for Redis as it starts."""
    client = make_redis(silent_server.url)
    CollectorRegistry(auto_describe=True).register(BacklogCollector(client, STREAM))
    assert silent_server.connections_made() == 0
    client.close()
