from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app import service
from app.db import SessionLocal
from app.models import Link


def test_create_and_redirect(client):
    r = client.post("/api/links", json={"url": "https://example.com/page"})
    assert r.status_code == 201
    body = r.json()
    assert len(body["code"]) == 7
    assert body["short_url"].endswith("/" + body["code"])

    r = client.get(f"/{body['code']}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/page"


def test_clicks_are_counted(client):
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]
    for _ in range(3):
        client.get(f"/{code}")
    assert client.get(f"/api/links/{code}").json()["clicks"] == 3


def test_custom_alias(client):
    r = client.post("/api/links", json={"url": "https://example.com", "alias": "my-link"})
    assert r.status_code == 201
    assert r.json()["code"] == "my-link"


def test_duplicate_alias_conflicts(client):
    client.post("/api/links", json={"url": "https://a.com", "alias": "taken"})
    r = client.post("/api/links", json={"url": "https://b.com", "alias": "taken"})
    assert r.status_code == 409


def test_reserved_alias_rejected(client):
    r = client.post("/api/links", json={"url": "https://a.com", "alias": "health"})
    assert r.status_code == 409


def test_invalid_url_rejected(client):
    assert client.post("/api/links", json={"url": "not a url"}).status_code == 422
    assert client.post("/api/links", json={"url": "ftp://x.com"}).status_code == 422


def test_invalid_alias_rejected(client):
    r = client.post("/api/links", json={"url": "https://a.com", "alias": "no spaces!"})
    assert r.status_code == 422


def test_unknown_code_404(client):
    assert client.get("/doesnotexist").status_code == 404


def test_expired_link_410(client):
    code = client.post(
        "/api/links", json={"url": "https://example.com", "expires_in_days": 1}
    ).json()["code"]
    with SessionLocal() as s:
        s.execute(
            update(Link)
            .where(Link.code == code)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        s.commit()
    assert client.get(f"/{code}").status_code == 410


def test_random_code_collision_retries(client, monkeypatch):
    # Force the first generated code to collide with an existing alias.
    client.post("/api/links", json={"url": "https://a.com", "alias": "AAAAAAA"})
    codes = iter(["AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr(service.base62, "random_code", lambda n: next(codes))
    r = client.post("/api/links", json={"url": "https://b.com"})
    assert r.status_code == 201
    assert r.json()["code"] == "BBBBBBB"
