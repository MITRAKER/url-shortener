"""Do clicks wait safely while the click worker is down, even across a Redis restart?

CI runs it in two halves against the Docker stack. The first half, run while
the worker is stopped, creates a link and clicks it: the clicks wait in the
Redis stream, uncounted. Then Redis restarts and the worker starts again. The
second half waits until the worker has counted every one of them. Only the
standard library is needed.

Usage:
    code="$(python -m scripts.check_clicks http://localhost:8000 click 5)"
    python -m scripts.check_clicks http://localhost:8000 expect "$code" 5
"""

import json
import sys
import time
from collections.abc import Sequence
from http import HTTPStatus

from scripts.smoke_test import TARGET, SmokeTestError, counted_clicks, expect, request

# The smoke test, just before, uses up the create limit (10 a minute) on
# purpose. So a 429 is expected here: wait as long as Retry-After says.
MAX_CREATE_ATTEMPTS = 3
MAX_WAIT_SECONDS = 10


def create(base_url: str) -> str:
    """A new link's code."""
    for _ in range(MAX_CREATE_ATTEMPTS):  # bounded (NASA/JPL rule 2)
        status, headers, body = request(base_url, "POST", "/api/links", {"url": TARGET})
        if status != HTTPStatus.TOO_MANY_REQUESTS:
            expect(status == HTTPStatus.CREATED, f"POST /api/links returned {status}: {body!r}")
            return str(json.loads(body)["code"])
        time.sleep(min(int(headers.get("retry-after", "1")), MAX_WAIT_SECONDS))
    raise SmokeTestError(f"still rate limited after {MAX_CREATE_ATTEMPTS} tries")


def click(base_url: str, times: int) -> str:
    """Click a new link `times` times, and check that the clicks are waiting
    in the stream, not counted: the worker must be stopped."""
    code = create(base_url)
    for _ in range(times):  # bounded by the caller's number
        status, _, _ = request(base_url, "GET", f"/{code}")
        expect(status == HTTPStatus.FOUND, f"GET /{code} returned {status}")
    status, _, body = request(base_url, "GET", f"/api/links/{code}")
    expect(status == HTTPStatus.OK, f"GET /api/links/{code} returned {status}")
    clicks = json.loads(body)["clicks"]
    # Counted already? Then they went straight to PostgreSQL, the way
    # clicks go when Redis can't take them, and this check would prove nothing.
    expect(clicks == 0, f"{clicks} clicks counted with the worker stopped")
    return code


def main(argv: Sequence[str]) -> int:
    try:
        match argv:
            case [base_url, "click", times]:
                print(click(base_url, int(times)))
            case [base_url, "expect", code, times]:
                clicks = counted_clicks(base_url, code, int(times))
                expect(clicks == int(times), f"expected {times} clicks, counted {clicks}")
                print(f"All {clicks} clicks counted")
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except (SmokeTestError, OSError, ValueError, KeyError) as exc:
        print(f"Click check FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
