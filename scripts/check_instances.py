"""Through the load balancer: do the requests reach every instance, and all succeed?

CI runs it against the Docker stack twice: with all three instances up, then
right after stopping one, to show that losing an instance loses no requests.
Only the standard library is needed.

Usage:
    python -m scripts.check_instances http://localhost:8000 3
"""

import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from http import HTTPStatus

from scripts.smoke_test import SmokeTestError, expect, request

REQUESTS = 30  # enough for round robin to visit every instance several times


def check(base_url: str, expected_instances: int) -> str:
    """Ask /health REQUESTS times; return a summary, or raise SmokeTestError."""
    answered_by: Counter[int] = Counter()
    slowest = 0.0
    for _ in range(REQUESTS):  # bounded (NASA/JPL rule 2)
        start = time.monotonic()
        status, _, body = request(base_url, "GET", "/health")
        slowest = max(slowest, time.monotonic() - start)
        expect(status == HTTPStatus.OK, f"GET /health returned {status}: {body!r}")
        answered_by[json.loads(body)["instance"]] += 1
    spread = ", ".join(f"instance {n}: {count}" for n, count in sorted(answered_by.items()))
    expect(
        len(answered_by) == expected_instances,
        f"expected {expected_instances} instances, got {len(answered_by)} ({spread})",
    )
    return f"{REQUESTS} requests, all answered ({spread}); slowest {slowest:.2f} s"


def main(argv: Sequence[str]) -> int:
    base_url = argv[0] if argv else "http://localhost:8000"
    expected = int(argv[1]) if len(argv) > 1 else 3
    try:
        summary = check(base_url, expected)
    except (SmokeTestError, OSError, ValueError, KeyError) as exc:
        print(f"Instance check FAILED: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
