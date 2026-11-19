import logging
import signal
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import redis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.cache import LinkCache
from app.config import DEFAULT_CODE_SECRET, settings
from app.db import SessionLocal
from app.ids import CODE_LENGTH, CodeCipher, LinkIds, parse_id
from app.instances import Lease
from app.limits import MAX_BODY_BYTES
from app.main import app
from app.models import Link
from app.schemas import MAX_URL_LENGTH
from app.service import MAX_ATTEMPTS
from tests.conftest import TEST_CODE_SECRET


def test_health(client: TestClient) -> None:
    # "instance": which instance answered (SQLite: always 0).
    assert client.get("/health").json() == {"status": "ok", "instance": 0}
    assert client.head("/health").status_code == 200


def test_create_and_redirect(client: TestClient) -> None:
    r = client.post("/api/links", json={"url": "https://example.com/page"})
    assert r.status_code == 201
    body = r.json()
    assert len(body["code"]) == CODE_LENGTH
    assert body["short_url"].endswith("/" + body["code"])
    assert body["clicks"] == 0
    assert body["expires_at"] is None

    r = client.get(f"/{body['code']}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/page"


def test_clicks_are_counted(client: TestClient) -> None:
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]
    for _ in range(3):
        client.get(f"/{code}")
    assert client.get(f"/api/links/{code}").json()["clicks"] == 3


def test_head_shows_the_target_without_counting_a_click(client: TestClient) -> None:
    code = client.post("/api/links", json={"url": "https://example.com/x"}).json()["code"]
    r = client.head(f"/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/x"
    assert r.content == b""
    assert client.get(f"/api/links/{code}").json()["clicks"] == 0
    assert client.head("/doesnotexist").status_code == 404


def test_custom_alias(client: TestClient) -> None:
    r = client.post("/api/links", json={"url": "https://example.com", "alias": "my-link"})
    assert r.status_code == 201
    assert r.json()["code"] == "my-link"


def test_duplicate_alias_conflicts(client: TestClient) -> None:
    client.post("/api/links", json={"url": "https://a.com", "alias": "taken"})
    r = client.post("/api/links", json={"url": "https://b.com", "alias": "taken"})
    assert r.status_code == 409


@pytest.mark.parametrize("alias", ["health", "API", "docs"])
def test_reserved_alias_rejected(client: TestClient, alias: str) -> None:
    r = client.post("/api/links", json={"url": "https://a.com", "alias": alias})
    assert r.status_code == 409


@pytest.mark.parametrize("url", ["not a url", "ftp://x.com", "javascript:alert(1)"])
def test_invalid_url_rejected(client: TestClient, url: str) -> None:
    assert client.post("/api/links", json={"url": url}).status_code == 422


def test_url_length_is_limited(client: TestClient) -> None:
    base = "https://example.com/"
    longest = base + "a" * (MAX_URL_LENGTH - len(base))
    assert client.post("/api/links", json={"url": longest}).status_code == 201
    r = client.post("/api/links", json={"url": longest + "a"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "url_too_long"


@pytest.mark.parametrize("char", ["é", "😀"])  # 2 and 4 bytes in UTF-8
def test_url_length_is_measured_as_stored(client: TestClient, char: str) -> None:
    # Stored percent-encoded in the path, 3 characters per UTF-8 byte: "é" is "%C3%A9".
    base = "https://example.com/"
    longest = base + char * ((MAX_URL_LENGTH - len(base)) // (3 * len(char.encode())))
    r = client.post("/api/links", json={"url": longest})
    assert r.status_code == 201
    assert len(r.json()["target_url"]) == MAX_URL_LENGTH
    assert len(client.get(f"/{r.json()['code']}").headers["location"]) == MAX_URL_LENGTH

    r = client.post("/api/links", json={"url": longest + char})  # under 2048 bytes as typed
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "url_too_long"  # as UrlConstraints reports it
    assert r.json()["detail"][0]["msg"] == f"URL should have at most {MAX_URL_LENGTH} characters"


def test_largest_valid_body_fits_under_the_size_limit(client: TestClient) -> None:
    # Worst case: every character of a 2048-character URL written as a JSON
    # escape (6 bytes each). It must still fit.
    base = "https://example.com/"
    escaped = "\\u0061" * (MAX_URL_LENGTH - len(base))
    body = f'{{"url": "{base}{escaped}", "alias": "{"x" * 32}"}}'.encode()
    assert len(body) < MAX_BODY_BYTES
    r = client.post("/api/links", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 201


def test_oversized_body_is_refused_before_reading(client: TestClient) -> None:
    body = b'{"url": "https://example.com/' + b"a" * MAX_BODY_BYTES + b'"}'
    r = client.post("/api/links", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.json() == {"detail": "Request body is too large"}


def test_oversized_body_without_content_length_is_refused(client: TestClient) -> None:
    chunks = iter([b'{"url": "https://example.com/', b"a" * MAX_BODY_BYTES, b'"}'])
    r = client.post("/api/links", content=chunks, headers={"content-type": "application/json"})
    assert "content-length" not in r.request.headers  # sent chunked
    assert r.status_code == 413


@pytest.mark.parametrize("alias", ["no spaces!", "ab", "x" * 33])
def test_invalid_alias_rejected(client: TestClient, alias: str) -> None:
    r = client.post("/api/links", json={"url": "https://a.com", "alias": alias})
    assert r.status_code == 422


def test_unknown_code_404(client: TestClient) -> None:
    assert client.get("/doesnotexist").status_code == 404
    assert client.get("/api/links/doesnotexist").status_code == 404


def test_expiry_is_reported(client: TestClient) -> None:
    body = client.post(
        "/api/links", json={"url": "https://example.com", "expires_in_days": 1}
    ).json()
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert timedelta(hours=23) < expires_at - datetime.now(UTC) <= timedelta(days=1)


def test_expired_link_410(client: TestClient, redis_client: redis.Redis) -> None:
    code = client.post(
        "/api/links", json={"url": "https://example.com", "expires_in_days": 1}
    ).json()["code"]
    with SessionLocal() as s:
        s.execute(
            update(Link)
            .where(Link.code == code)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        s.commit()
    # We changed the database behind the cache's back, so drop the cached copy
    # (test_cache.py shows what happens if you forget).
    redis_client.delete(LinkCache.key(code))
    assert client.get(f"/{code}").status_code == 410


def _stored_id(code: str) -> int | None:
    with SessionLocal() as s:
        return s.scalar(select(Link.id).where(Link.code == code))


def test_a_generated_code_is_the_scrambled_id(client: TestClient) -> None:
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]
    link_id = _stored_id(code)
    assert link_id is not None
    assert CodeCipher(TEST_CODE_SECRET).decode(code) == link_id
    assert parse_id(link_id).instance_id == 0


def _hand_out(monkeypatch: pytest.MonkeyPatch, pairs: Iterator[tuple[int, str]]) -> None:
    """From now on, new links get these (ID, code) pairs, in this order."""
    monkeypatch.setattr(LinkIds, "new", lambda _self: next(pairs))


def test_a_code_that_clashes_with_an_alias_is_replaced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Someone chose, as their alias, the code our next ID scrambles to. The
    database refuses the duplicate code, and the link gets the next ID."""
    client.post("/api/links", json={"url": "https://a.com", "alias": "AAAAAAAA"})
    _hand_out(monkeypatch, iter([(1, "AAAAAAAA"), (2, "BBBBBBBB")]))
    r = client.post("/api/links", json={"url": "https://b.com"})
    assert r.status_code == 201
    assert r.json()["code"] == "BBBBBBBB"
    assert _stored_id("BBBBBBBB") == 2
    assert "clashed with an existing row, retrying" in caplog.text


def test_an_id_that_is_taken_is_replaced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two instances with the same number would make the same IDs. The
    primary key refuses the second one, and nothing is overwritten."""
    first = client.post("/api/links", json={"url": "https://a.com/first"}).json()["code"]
    taken = _stored_id(first)
    assert taken is not None
    _hand_out(monkeypatch, iter([(taken, "CCCCCCCC"), (taken + 1, "DDDDDDDD")]))
    r = client.post("/api/links", json={"url": "https://b.com/second"})
    assert r.json()["code"] == "DDDDDDDD"
    assert client.get(f"/{first}").headers["location"] == "https://a.com/first"


def test_an_alias_survives_an_id_clash(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The INSERT failed, but not because of the alias: retry, don't report it taken."""
    first = client.post("/api/links", json={"url": "https://a.com"}).json()["code"]
    taken = _stored_id(first)
    assert taken is not None
    _hand_out(monkeypatch, iter([(taken, "unused-1"), (taken + 1, "unused-2")]))
    r = client.post("/api/links", json={"url": "https://b.com", "alias": "my-alias"})
    assert r.status_code == 201
    assert r.json()["code"] == "my-alias"


def test_gives_up_after_max_attempts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client.post("/api/links", json={"url": "https://a.com", "alias": "AAAAAAAA"})
    # Exactly MAX_ATTEMPTS pairs: asking for one more would fail the test.
    _hand_out(monkeypatch, iter([(n, "AAAAAAAA") for n in range(1, MAX_ATTEMPTS + 1)]))
    r = client.post("/api/links", json={"url": "https://b.com"})
    assert r.status_code == 503
    assert r.json() == {"detail": "Could not allocate a code"}
    assert caplog.text.count("retrying") == MAX_ATTEMPTS


# --- Startup ----------------------------------------------------------------


def test_startup_makes_the_ids() -> None:
    """Without the tests' stand-in, the app makes its own IDs at startup."""
    with TestClient(app) as c:
        ids = app.state.ids
        assert isinstance(ids, LinkIds)
        assert c.get("/health").json() == {"status": "ok", "instance": ids.snowflake.instance_id}
        assert ids.cipher.decode(CodeCipher(TEST_CODE_SECRET).encode(42)) == 42


@pytest.mark.parametrize(
    ("secret", "warned"), [(DEFAULT_CODE_SECRET, True), (TEST_CODE_SECRET, False)]
)
def test_startup_warns_about_the_public_default_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, secret: str, warned: bool
) -> None:
    monkeypatch.setattr(settings, "code_secret", SecretStr(secret))
    with caplog.at_level(logging.WARNING), TestClient(app):
        pass
    assert ("CODE_SECRET is not set" in caplog.text) is warned


def test_an_instance_that_lost_its_number_stops_making_ids_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog (app/instances.py) finds the lease gone. Instead of risking
    duplicate IDs, the instance refuses new links and asks to be stopped."""
    signals: list[signal.Signals] = []
    monkeypatch.setattr(signal, "raise_signal", signals.append)
    monkeypatch.setattr(Lease, "is_held", lambda _self: False)
    with TestClient(app) as c:
        for _ in range(500):  # the first check runs right after startup
            if signals:
                break
            time.sleep(0.01)
        assert signals == [signal.SIGTERM]  # what `docker stop` sends
        r = c.post("/api/links", json={"url": "https://a.com"})
        assert r.status_code == 503
        assert c.get("/health").status_code == 200  # still answers while it stops
