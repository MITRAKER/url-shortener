import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from fastapi.testclient import TestClient

from app.main import app
from app.ratelimit import Rate, TokenBucket


def test_parse_rates() -> None:
    assert Rate.parse("10/minute") == Rate(10, 60)
    assert Rate.parse(" 5 / second ") == Rate(5, 1)
    assert Rate.parse("1000/hour").per_second == pytest.approx(1000 / 3600)


@pytest.mark.parametrize("text", ["10", "ten/minute", "10/day", "0/minute", "-1/second"])
def test_invalid_rates_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match=r"invalid rate|at least 1"):
        Rate.parse(text)


def test_create_is_limited_to_10_per_minute(client: TestClient) -> None:
    for i in range(10):
        r = client.post("/api/links", json={"url": f"https://example.com/{i}"})
        assert r.status_code == 201
    r = client.post("/api/links", json={"url": "https://example.com/one-too-many"})
    assert r.status_code == 429
    # One token comes back every 60/10 = 6 seconds.
    assert 1 <= int(r.headers["retry-after"]) <= 6
    assert r.headers["x-ratelimit-limit"] == "10"
    assert r.headers["x-ratelimit-remaining"] == "0"


def test_each_client_ip_has_its_own_bucket(client: TestClient) -> None:
    for i in range(10):
        client.post("/api/links", json={"url": f"https://example.com/{i}"})
    assert client.post("/api/links", json={"url": "https://a.com"}).status_code == 429

    other = TestClient(app, client=("203.0.113.7", 50000))
    assert other.post("/api/links", json={"url": "https://a.com"}).status_code == 201


def test_reads_have_their_own_bucket(client: TestClient) -> None:
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]
    for i in range(9):
        client.post("/api/links", json={"url": f"https://example.com/{i}"})
    # The create bucket is now empty, but redirects use the separate "read" bucket.
    assert client.get(f"/{code}").status_code == 302


def test_bucket_refills_over_time(redis_client: redis.Redis) -> None:
    bucket = TokenBucket(redis_client)
    rate = Rate(5, 1)  # 5 per second: one token every 0.2 s
    assert all(bucket.take("t", rate).allowed for _ in range(5))  # a burst of 5
    denied = bucket.take("t", rate)
    assert not denied.allowed
    assert denied.retry_after == 1

    time.sleep(0.3)  # earns 1.5 tokens
    assert bucket.take("t", rate).allowed
    assert not bucket.take("t", rate).allowed


def test_remaining_counts_down(redis_client: redis.Redis) -> None:
    bucket = TokenBucket(redis_client)
    rate = Rate(3, 3600)
    assert [bucket.take("t", rate).remaining for _ in range(3)] == [2, 1, 0]


def test_cost_must_be_positive(redis_client: redis.Redis) -> None:
    with pytest.raises(ValueError, match="cost"):
        TokenBucket(redis_client).take("t", Rate(3, 60), cost=0)


def test_idle_buckets_expire(redis_client: redis.Redis) -> None:
    TokenBucket(redis_client).take("t", Rate(10, 60))
    # A full refill takes 60 s, so the key may be deleted after about 61 s.
    assert 0 < redis_client.ttl("t") <= 61


def test_exactly_limit_requests_win_under_concurrency(redis_client: redis.Redis) -> None:
    """50 threads race for 10 tokens. Because the Lua script is atomic, exactly
    10 succeed. A read-then-write version in Python could let more through."""
    bucket = TokenBucket(redis_client)
    rate = Rate(10, 3600)
    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(lambda _: bucket.take("race", rate).allowed, range(50)))
    assert sum(results) == 10


def test_rate_limiting_fails_open_when_redis_is_down(client_with_redis_down: TestClient) -> None:
    for i in range(15):  # more than the limit of 10
        r = client_with_redis_down.post("/api/links", json={"url": f"https://a.com/{i}"})
        assert r.status_code == 201


def test_rate_limiting_fails_open_when_redis_hangs(client_with_redis_hung: TestClient) -> None:
    """Redis accepts connections but never answers. The first calls time out,
    then the circuit breaker skips Redis, so requests keep working and stay fast."""
    start = time.perf_counter()
    for i in range(15):
        r = client_with_redis_hung.post("/api/links", json={"url": f"https://a.com/{i}"})
        assert r.status_code == 201
    # 3 timeouts of 0.25 s open the circuit; without it: 15 x 2 x 0.25 s = 7.5 s.
    assert time.perf_counter() - start < 3
