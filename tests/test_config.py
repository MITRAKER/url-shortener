import pytest
import redis
from pydantic import ValidationError

from app.config import APP_REDIS_OPTIONS, Settings
from app.redis_client import make_redis


@pytest.mark.parametrize(
    "url",
    [
        "",  # no Redis: the app runs without cache and rate limits
        "redis://localhost:6379/0",
        "rediss://user:secret@cache.example.com:6380/1",  # TLS
        "unix:///run/redis/redis.sock",
        "redis://127.0.0.1:1/0",  # nothing listens there: the check never connects
        "redis://:P%40ssw0rd%232026@localhost:6379/0",  # '@' and '#' percent-encoded
        # The password in the query, where an '@' needs no encoding.
        "redis://localhost:6379/0?password=p@ss",
        "unix:///run/redis/redis.sock?db=0&password=p@ss",
        "unix://default@/run/redis/redis.sock?db=0&password=p@ss",
        "unix:///run/x@1/redis.sock",  # an '@' in a socket path
        "rediss://cache.example.com:6380/0?ssl_cert_reqs=none&ssl_ca_certs=/etc/ssl/ca.pem",
        "redis://localhost:6379/0?health_check_interval=10&client_name=shortener",
    ],
)
def test_valid_redis_urls_are_accepted(url: str) -> None:
    assert Settings(redis_url=url).redis_url == url


@pytest.mark.parametrize(
    "url",
    [
        "localhost:6379",  # no scheme
        "redis//redis:6379/0",  # missing colon
        "http://redis:6379/0",  # wrong scheme
        "redis://redis:port/0",  # port is not a number
        # A query argument that a connection does not take, or a value it
        # refuses: the check used to pass them, and the first connection failed.
        "redis://redis:6379/0?dbb=1",  # a typo for db
        "redis://redis:6379/0?ssl_cert_reqs=none",  # TLS options need rediss://
        "rediss://redis:6380/0?ssl_cert_reqs=None",  # "none", "optional" or "required"
    ],
)
def test_malformed_redis_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="redis_url"):
        Settings(redis_url=url)


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("socket_timeout=30", "socket_timeout"),  # a hung Redis would hold each request 30 s
        ("max_connections=1000", "max_connections"),  # the pool has a fixed size
        ("retry_on_timeout=true", "retry_on_timeout"),  # a timeout is never retried
        ("protocol=3", "protocol"),  # the worker reads RESP2 replies
        ("encoding_errors=strict", "encoding_errors"),  # bad bytes would crash the worker
        ("db=1&socket%5Ftimeout=30", "socket_timeout"),  # percent-encoded, as redis-py reads it
    ],
)
def test_the_url_may_not_override_what_the_app_sets(query: str, name: str) -> None:
    """redis-py lets the URL's query arguments win over the arguments that
    make_redis passes, so a URL could quietly undo the limits of lesson 3."""
    with pytest.raises(ValidationError, match=f"may not set what the app sets: {name}"):
        Settings(redis_url=f"redis://redis:6379/0?{query}")


def test_the_url_may_override_nothing_that_make_redis_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If make_redis starts to set something new, the check must refuse it in
    the URL too."""
    passed: dict[str, object] = {}

    def spy(_url: str, **kwargs: object) -> None:
        passed.update(kwargs)

    monkeypatch.setattr(redis.Redis, "from_url", spy)
    make_redis("redis://redis:6379/0")
    assert passed
    assert set(passed) <= APP_REDIS_OPTIONS


def test_malformed_redis_url_stops_the_app_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings are read when the app starts, so a typo in the environment
    fails loudly there, before the app reports itself healthy. It must never
    reach the first request, where it would turn every response into a 500."""
    monkeypatch.setenv("REDIS_URL", "localhost:6379")
    with pytest.raises(ValidationError, match="redis_url"):
        Settings()


@pytest.mark.parametrize(
    "url",
    [
        "redis//:hunter2@redis:6379/0",  # missing colon
        "http://:hunter2@redis:6379/0",  # wrong scheme
        "redis://:hunter2@redis:port/0",  # port is not a number
        # A '#', '/' or '?' that is not percent-encoded: the parser stops
        # there and reads the start of the password as the port.
        "redis://:hun#ter2@redis:6379/0",
        "redis://:hun/ter2@redis:6379/0",
        "redis://:hun?ter2@redis:6379/0",
        # A full-width '#': Python's URL parser quotes all of ":password@host:port".
        "redis://:hun\uff03ter2@redis:6379/0",
        # The same, but the start of the password is a valid port number: the
        # URL parses, without the password, and the app would run without Redis.
        "redis://:4711#hunter2@redis:6379/0",
        "redis://:4711/hunter2@redis:6379/0",
        "redis://:4711?hunter2@redis:6379/0",
        # The same with an '@' before the '#', '/' or '?': the URL parses, and
        # redis-py would take "ter2" for the host and "hun" for the password.
        "redis://:hun@ter2#4711@redis:6379/0",
        "redis://default:hun@ter2/4711@redis:6379/0",
        "redis://:hun@ter2?4711@redis:6379/0",
        # A socket path may hold an '@', but not after user info: there it
        # may be the '@' that ends the password.
        "unix://:hun/ter2@/run/redis/redis.sock",
        # In the query, redis-py drops a '#' and everything after it.
        "redis://redis:6379/0?password=hun#ter2",
        "redis://redis:6379/0?password=hunter2#",
    ],
)
def test_error_for_a_malformed_redis_url_does_not_show_the_password(url: str) -> None:
    """The error is printed when the app stops, so it ends up in the logs.
    Not even a part of the password may appear in it."""
    with pytest.raises(ValidationError, match="redis_url") as error:
        Settings(redis_url=url)
    assert "hun" not in str(error.value)
    assert "ter2" not in str(error.value)
    assert "4711" not in str(error.value)


def test_a_short_code_secret_stops_the_app_without_showing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_SECRET", "hunter2-hunter2")  # 15 characters, one short
    with pytest.raises(ValidationError, match="code_secret") as error:
        Settings()
    assert "hunter2" not in str(error.value)


def test_the_code_secret_is_hidden_when_settings_are_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_SECRET", "a-long-and-private-secret")
    settings = Settings()
    assert "a-long-and-private-secret" not in repr(settings)
    assert settings.code_secret.get_secret_value() == "a-long-and-private-secret"


@pytest.mark.parametrize(
    ("value", "proxies"),
    [
        ("", []),  # no proxy in front: trust nobody
        ("10.89.0.10", ["10.89.0.10"]),
        ("10.89.0.0/24", ["10.89.0.0/24"]),  # a whole network
        (" 10.0.0.1 ,, fd00::/8 ", ["10.0.0.1", "fd00::/8"]),  # IPv6 too; blanks skipped
    ],
)
def test_trusted_proxies_are_addresses_or_networks(value: str, proxies: list[str]) -> None:
    assert Settings(trusted_proxies=value).trusted_proxy_list == proxies


@pytest.mark.parametrize(
    "value",
    [
        "*",  # trust everyone: anyone could choose their own address
        "nginx",  # a host name: not resolved, so it would never match
        "10.89.0.300",  # a typo
        "10.89.0.0/33",
        "10.89.0.10/24",  # host bits set: uvicorn would ignore it, and trust nobody
    ],
)
def test_anything_else_in_trusted_proxies_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="trusted_proxies"):
        Settings(trusted_proxies=value)
