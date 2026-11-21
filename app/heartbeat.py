"""Is the click worker alive? A heartbeat file answers.

The worker touches the file on every round of its loop (app/worker.py); the
container's health check runs `python -m app.heartbeat`, which asks how long
ago that was: exit code 0 if recently, 1 if not. Docker only marks a
container that fails its health check as unhealthy, so the worker's own
watchdog asks the same question, and ends the worker when its loop is stuck.

This module uses nothing but the standard library, on purpose. The health
check runs every 10 seconds, and importing the worker (SQLAlchemy, Pydantic,
redis-py) takes about a second of CPU time: we measured 1.0 s for a check
that imported the worker, 0.15 s for this one.
"""

import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

HEARTBEAT: Final = Path(tempfile.gettempdir()) / "click-worker-alive"
# Alive if the last round began less than this long ago. Every wait in a
# round has a limit far below it (app/worker.py), so only a stuck loop is late.
ALIVE_SECONDS: Final = 30.0


def is_alive(heartbeat: Path, now: Callable[[], float] = time.time) -> bool:
    """Has the worker gone round its loop recently? (Alive, not necessarily
    making progress: a worker waiting for the database to come back is alive.)"""
    try:
        return now() - heartbeat.stat().st_mtime < ALIVE_SECONDS
    except FileNotFoundError:
        return False


def main() -> int:
    return 0 if is_alive(HEARTBEAT) else 1


if __name__ == "__main__":
    sys.exit(main())
