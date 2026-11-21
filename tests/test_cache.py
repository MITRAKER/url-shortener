import time
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.cache import KEY_PREFIX, CachedLink, LinkCache, Lookup
from app.db import SessionLocal
from app.models import Link
from app.redis_client import RedisGateway, make_redis
from tests.fakes import SilentServer


def create(client: TestClient, **body: Any) -> str:
    r = client.post("/api/links", json={"url": "https://example.com", **body})
    assert r.status_code == 201, r.text
    code: str = r.json()["code"]
    return code


def make_cache(redis_client: redis.Redis) -> LinkCache:
    return LinkCache(RedisGateway(redis_client), ttl=3600, negative_ttl=60)


def refuse_writes(*_args: object, **_kwargs: object) -> NoReturn:
    """Stands in for `Redis.set` when Redis answers reads but refuses writes,
    as it does when its memory is full and its policy is noeviction."""
    raise redis.exceptions.OutOfMemoryError("command not allowed when used memory > 'maxmemory'")


def test_new_link_is_cached_on_create(client: TestClient) -> None:
    # Write-through: the very first redirect is already a cache hit.
    code = create(client)
    r = client.get(f"/{code}")
    assert (r.status_code, r.headers["x-cache"]) == (302, "HIT")


def test_miss_then_hit(client: TestClient, redis_client: redis.Redis) -> None:
    code = create(client)
    redis_client.delete(LinkCache.key(code))  # as if the entry had expired

    first = client.get(f"/{code}")
    second = client.get(f"/{code}")
    assert (first.status_code, first.headers["x-cache"]) == (302, "MISS")
    assert (second.status_code, second.headers["x-cache"]) == (302, "HIT")


def test_cached_entry_has_a_ttl(client: TestClient, redis_client: redis.Redis) -> None:
    code = create(client)
    ttl = redis_client.ttl(LinkCache.key(code))
    assert 3600 * 0.9 - 1 <= ttl <= 3600 * 1.1


def test_ttl_jitter_stays_within_ten_percent(redis_client: redis.Redis) -> None:
    cache = LinkCache(RedisGateway(redis_client), ttl=1000, negative_ttl=60)
    ttls = {cache.ttl_with_jitter() for _ in range(200)}
    assert min(ttls) >= 900
    assert max(ttls) <= 1100
    assert len(ttls) > 1  # really random, so entries don't all expire together


@pytest.mark.parametrize(
    ("ttl", "negative_ttl", "jitter"), [(0, 60, 0.1), (60, 0, 0.1), (60, 60, 1.0), (60, 60, -0.1)]
)
def test_invalid_settings_are_rejected(
    redis_client: redis.Redis, ttl: int, negative_ttl: int, jitter: float
) -> None:
    with pytest.raises(ValueError, match="must be"):
        LinkCache(RedisGateway(redis_client), ttl=ttl, negative_ttl=negative_ttl, jitter=jitter)


def test_cache_hit_does_not_read_the_database(client: TestClient) -> None:
    """Proves redirects are served from Redis, and shows the price: if the
    database changes behind the cache's back, the cache serves stale data
    until the entry expires. Every code path that changes a link must
    therefore update or delete its cache entry."""
    code = create(client, alias="stale")
    with SessionLocal() as s:
        s.execute(update(Link).where(Link.code == code).values(target_url="https://changed.com"))
        s.commit()
    r = client.get(f"/{code}")
    assert r.headers["x-cache"] == "HIT"
    assert r.headers["location"] == "https://example.com/"  # the old, cached target


def test_clicks_are_counted_on_cache_hits(client: TestClient) -> None:
    code = create(client)
    for _ in range(3):
        assert client.get(f"/{code}").headers["x-cache"] == "HIT"
    assert client.get(f"/api/links/{code}").json()["clicks"] == 3


def test_unknown_code_is_negatively_cached(client: TestClient, redis_client: redis.Redis) -> None:
    first = client.get("/nothere")
    assert (first.status_code, first.headers["x-cache"]) == (404, "MISS")
    key = LinkCache.key("nothere")
    assert redis_client.get(key) == ""
    assert 0 < redis_client.ttl(key) <= 60

    second = client.get("/nothere")
    assert (second.status_code, second.headers["x-cache"]) == (404, "HIT")


def test_creating_a_link_replaces_a_not_found_entry(client: TestClient) -> None:
    assert client.get("/later").status_code == 404  # now cached as "not found"
    create(client, alias="later")
    r = client.get("/later")
    assert (r.status_code, r.headers["x-cache"]) == (302, "HIT")


def test_failed_write_through_leaves_the_not_found_entry(
    client: TestClient, redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The price of carrying on without Redis: if the write-through fails, the
    "not found" entry stays, and the new link answers 404 until the entry
    expires (at most cache_negative_ttl_seconds, 60 s by default)."""
    assert client.get("/later").status_code == 404  # now cached as "not found"
    with monkeypatch.context() as patch:
        patch.setattr(redis_client, "set", refuse_writes)
        create(client, alias="later")  # 201, but the write-through fails
    r = client.get("/later")
    assert (r.status_code, r.headers["x-cache"]) == (404, "HIT")
    assert 0 < redis_client.ttl(LinkCache.key("later")) <= 60


def test_not_found_never_overwrites_a_real_entry(redis_client: redis.Redis) -> None:
    """The race this guards against:
    1. request A misses the cache and finds nothing in the database
    2. request B creates the link and caches it
    3. A, finishing late, caches "not found" on top of B's entry
    set_not_found uses SET NX, so step 3 does nothing."""
    cache = make_cache(redis_client)
    cache.set("abc", CachedLink("https://example.com", None))
    cache.set_not_found("abc")
    assert cache.get("abc") == CachedLink("https://example.com", None)


def test_writes_report_whether_redis_stored_them(redis_client: redis.Redis) -> None:
    cache = make_cache(redis_client)
    assert cache.set_not_found("abc") is True
    assert cache.set_not_found("abc") is True  # another request cached "not found" already
    assert cache.set("abc", CachedLink("https://example.com", None)) is True
    assert cache.set_not_found("abc") is False  # SET NX: the real entry stays


def test_writes_report_a_failure(
    redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = make_cache(redis_client)
    monkeypatch.setattr(redis_client, "set", refuse_writes)
    assert cache.set("abc", CachedLink("https://example.com", None)) is False
    assert cache.set_not_found("abc") is False


def test_answer_that_was_not_cached_is_a_bypass(
    client: TestClient, redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MISS means "now cached". If the write fails, the next request misses
    again, so the header must not say MISS."""
    code = create(client)
    redis_client.delete(LinkCache.key(code))  # as if the entry had expired
    monkeypatch.setattr(redis_client, "set", refuse_writes)

    found = client.get(f"/{code}")
    unknown = client.get("/nothere")

    assert (found.status_code, found.headers["x-cache"]) == (302, "BYPASS")
    assert (unknown.status_code, unknown.headers["x-cache"]) == (404, "BYPASS")
    assert redis_client.keys(KEY_PREFIX + "*") == []  # really nothing cached


def test_expiry_survives_the_round_trip(redis_client: redis.Redis) -> None:
    cache = make_cache(redis_client)
    expires = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    cache.set("abc", CachedLink("https://example.com", expires))
    assert cache.get("abc") == CachedLink("https://example.com", expires)


def test_expired_link_from_cache_is_410(client: TestClient, redis_client: redis.Redis) -> None:
    code = create(client)
    past = datetime.now(UTC) - timedelta(seconds=1)
    make_cache(redis_client).set(code, CachedLink("https://example.com", past))
    r = client.get(f"/{code}")
    assert (r.status_code, r.headers["x-cache"]) == (410, "HIT")


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        '{"exp": null}',  # no url
        '{"url": "x", "exp": "soon"}',  # not a number
        '{"url": "x", "exp": 1e300}',  # a date far beyond the year 9999
        '{"url": "x", "exp": -1e20}',
        '{"url": "x", "exp": Infinity}',
        '{"url": "x", "exp": NaN}',
    ],
)
def test_unreadable_entry_is_treated_as_a_miss(redis_client: redis.Redis, bad: str) -> None:
    redis_client.set(LinkCache.key("abc"), bad)
    assert make_cache(redis_client).get("abc") is Lookup.MISS


def test_unreadable_entry_is_repaired_by_the_next_redirect(
    client: TestClient, redis_client: redis.Redis
) -> None:
    code = create(client)
    redis_client.set(LinkCache.key(code), '{"url": "x", "exp": 1e300}')
    r = client.get(f"/{code}")
    assert (r.status_code, r.headers["x-cache"]) == (302, "MISS")
    assert client.get(f"/{code}").headers["x-cache"] == "HIT"


def test_impossible_codes_never_reach_cache_or_database(
    client: TestClient, redis_client: redis.Redis
) -> None:
    for path in ["/" + "a" * 33, "/ab", "/bad.code", "/semi;colon"]:
        r = client.get(path)
        assert r.status_code == 404
        assert "x-cache" not in r.headers
    # Only the rate limiter's bucket was written, no cache entries.
    assert redis_client.keys(KEY_PREFIX + "*") == []


def test_works_without_redis(client_without_redis: TestClient) -> None:
    code = create(client_without_redis)
    r = client_without_redis.get(f"/{code}")
    assert (r.status_code, r.headers["x-cache"]) == (302, "BYPASS")


def test_survives_a_redis_outage(client_with_redis_down: TestClient) -> None:
    """Redis refuses connections: cache and rate limiter both fail open."""
    c = client_with_redis_down
    code = create(c)
    r = c.get(f"/{code}")
    stats = c.get(f"/api/links/{code}")

    assert (r.status_code, r.headers["x-cache"]) == (302, "BYPASS")
    assert stats.json()["clicks"] == 1


def test_survives_a_hung_redis_quickly(
    client_with_redis_hung: TestClient, silent_server: SilentServer
) -> None:
    """Redis accepts connections but never answers, the worst kind of outage:
    every call waits for the full timeout. The circuit breaker opens after 3
    failed calls, and from then on requests don't touch Redis at all."""
    c = client_with_redis_hung
    code = create(c)  # rate limiter: timeout 1; write-through: timeout 2
    first = c.get(f"/{code}")  # rate limiter: timeout 3, circuit opens; cache skipped
    assert silent_server.connections_made() == 3

    start = time.perf_counter()
    later = [c.get(f"/{code}") for _ in range(5)]
    elapsed = time.perf_counter() - start

    assert silent_server.connections_made() == 0  # Redis is not called any more
    assert all((r.status_code, r.headers["x-cache"]) == (302, "BYPASS") for r in [first, *later])
    assert elapsed < 1  # without the breaker: 5 requests x 2 calls x 0.25 s = 2.5 s


def test_not_found_value_is_distinct_from_a_miss(redis_client: redis.Redis) -> None:
    cache = make_cache(redis_client)
    assert cache.get("nope") is Lookup.MISS
    cache.set_not_found("nope")
    assert cache.get("nope") is Lookup.NOT_FOUND


def test_unknown_code_with_redis_down(client_with_redis_down: TestClient) -> None:
    r = client_with_redis_down.get("/nothere")
    assert (r.status_code, r.headers["x-cache"]) == (404, "BYPASS")


def test_redis_down_means_no_write_after_a_failed_read(silent_server: SilentServer) -> None:
    """If the read just failed, the write would only wait for another timeout."""
    gateway = RedisGateway(make_redis(silent_server.url))
    cache = LinkCache(gateway, ttl=60, negative_ttl=60)
    assert cache.get("abc") is Lookup.UNAVAILABLE
    assert silent_server.connections_made() == 1  # no retry on a timeout
    gateway.client.close()
