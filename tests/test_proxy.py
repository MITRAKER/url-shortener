"""Behind Nginx: whose address is the client's? (ProxyHeadersMiddleware in app/main.py)

Every request reaches the app from Nginx, so without X-Forwarded-For all
clients would share Nginx's address, and its rate limit. But anyone can write
that header, so it counts only on requests that come from a trusted proxy."""

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import settings
from app.main import app
from tests.fakes import NGINX

LIMIT = 10  # link creations per client per minute (the default)
SOMEONE = "203.0.113.7"
SOMEONE_ELSE = "203.0.113.8"


def create(client: TestClient, forwarded_for: str | None = None) -> int:
    headers = {} if forwarded_for is None else {"X-Forwarded-For": forwarded_for}
    return client.post("/api/links", json={"url": "https://a.com"}, headers=headers).status_code


def use_up_the_limit(client: TestClient, forwarded_for: str | None = None) -> None:
    assert [create(client, forwarded_for) for _ in range(LIMIT)] == [201] * LIMIT
    assert create(client, forwarded_for) == 429


# "client" sets up an empty database and Redis; these tests make their own clients.
@pytest.mark.usefixtures("client")
def test_behind_the_proxy_each_client_has_its_own_limit() -> None:
    nginx = TestClient(app, client=(NGINX, 40000))
    use_up_the_limit(nginx, SOMEONE)
    assert create(nginx, SOMEONE_ELSE) == 201


@pytest.mark.usefixtures("client")
def test_the_header_is_ignored_from_anyone_else() -> None:
    """A client talking to the app directly cannot dodge its limit by
    writing a new address in the header each time."""
    direct = TestClient(app, client=(SOMEONE, 40000))
    for n in range(LIMIT):
        assert create(direct, f"198.51.100.{n}") == 201
    assert create(direct, "198.51.100.99") == 429


@pytest.mark.usefixtures("client")
def test_an_address_put_in_front_by_the_client_is_ignored() -> None:
    """A proxy that appends to the header (most do; our Nginx replaces it)
    turns a forged header into "forged, real". The middleware reads from the
    right, skipping trusted proxies, so the forged address never counts."""
    nginx = TestClient(app, client=(NGINX, 40000))
    use_up_the_limit(nginx, SOMEONE)
    assert create(nginx, f"198.51.100.1, {SOMEONE}") == 429


def middleware_options(middleware: object) -> list[dict[str, object]]:
    """The options given to each use of `middleware` in app/main.py."""
    return [dict(m.kwargs) for m in app.user_middleware if m.cls is middleware]


def test_the_middleware_trusts_exactly_the_configured_proxies() -> None:
    assert middleware_options(ProxyHeadersMiddleware) == [
        {"trusted_hosts": settings.trusted_proxy_list}
    ]
    assert settings.trusted_proxy_list == [NGINX]
