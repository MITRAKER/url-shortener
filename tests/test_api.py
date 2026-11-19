from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import base62
from app.db import SessionLocal
from app.limits import MAX_BODY_BYTES
from app.models import Link
from app.schemas import MAX_URL_LENGTH


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.head("/health").status_code == 200


def test_create_and_redirect(client: TestClient) -> None:
    r = client.post("/api/links", json={"url": "https://example.com/page"})
    assert r.status_code == 201
    body = r.json()
    assert len(body["code"]) == 7
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
    assert client.post("/api/links", json={"url": longest + "a"}).status_code == 422


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
    assert f"at most {MAX_URL_LENGTH} characters" in r.json()["detail"][0]["msg"]


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


def test_expired_link_410(client: TestClient) -> None:
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
    assert client.get(f"/{code}").status_code == 410


def test_random_code_collision_retries(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the first generated code to collide with an existing alias.
    client.post("/api/links", json={"url": "https://a.com", "alias": "AAAAAAA"})
    codes = iter(["AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr(base62, "random_code", lambda _length: next(codes))
    r = client.post("/api/links", json={"url": "https://b.com"})
    assert r.status_code == 201
    assert r.json()["code"] == "BBBBBBB"


def test_gives_up_after_max_attempts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/links", json={"url": "https://a.com", "alias": "AAAAAAA"})
    monkeypatch.setattr(base62, "random_code", lambda _length: "AAAAAAA")
    r = client.post("/api/links", json={"url": "https://b.com"})
    assert r.status_code == 503
