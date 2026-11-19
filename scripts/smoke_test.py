"""End-to-end smoke test against a running server, using only the standard
library. CI runs it after `docker compose up` to prove the real containers work.

Usage:
    python scripts/smoke_test.py http://localhost:8000
"""

import http.client
import json
import sys
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 5  # every network call has an upper bound (NASA/JPL rule 2)
TARGET = "https://example.com/smoke-test"
# Default create limit is 10/minute, so 20 attempts must hit it (bounded loop).
MAX_CREATE_ATTEMPTS = 20


class SmokeTestError(Exception):
    pass


def request(
    base_url: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request without following redirects: (status, headers, body)."""
    url = urlsplit(base_url)
    conn = http.client.HTTPConnection(url.hostname or "localhost", url.port, TIMEOUT_SECONDS)
    try:
        payload = None if body is None else json.dumps(body).encode()
        headers = {} if body is None else {"Content-Type": "application/json"}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestError(message)


def run(base_url: str) -> None:
    status, _, _ = request(base_url, "GET", "/health")
    expect(status == HTTPStatus.OK, f"GET /health returned {status}")

    status, _, body = request(base_url, "POST", "/api/links", {"url": TARGET})
    expect(status == HTTPStatus.CREATED, f"POST /api/links returned {status}: {body!r}")
    code = json.loads(body)["code"]

    status, headers, _ = request(base_url, "GET", f"/{code}")
    expect(status == HTTPStatus.FOUND, f"GET /{code} returned {status}, expected 302")
    expect(headers.get("location") == TARGET, f"redirected to {headers.get('location')}")
    # Proves Redis is connected: new links are cached on create.
    expect(headers.get("x-cache") == "HIT", f"X-Cache was {headers.get('x-cache')}")

    status, _, body = request(base_url, "GET", f"/api/links/{code}")
    expect(status == HTTPStatus.OK, f"GET /api/links/{code} returned {status}")
    expect(json.loads(body)["clicks"] == 1, f"click was not counted: {body!r}")

    expect(rate_limit_kicks_in(base_url), "never got 429 Too Many Requests")


def rate_limit_kicks_in(base_url: str) -> bool:
    for _ in range(MAX_CREATE_ATTEMPTS):
        status, headers, _ = request(base_url, "POST", "/api/links", {"url": TARGET})
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            return headers.get("retry-after", "").isdigit()
    return False


def main(argv: Sequence[str]) -> int:
    base_url = argv[0] if argv else "http://localhost:8000"
    try:
        run(base_url)
    except (SmokeTestError, OSError, ValueError, KeyError) as exc:
        print(f"Smoke test FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Smoke test passed against {base_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
