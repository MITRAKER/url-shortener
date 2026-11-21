"""Is the click worker alive? A heartbeat file answers.

The worker touches the file as every round of its loop begins, and after
each step of a round (app/worker.py); the container's health check runs
`python -m app.heartbeat`, which asks how long ago the last touch was: exit
code 0 if recently, 1 if not. Docker only marks a container that fails its
health check as unhealthy, so the worker's own watchdog asks the same
question, and ends the worker when its loop is stuck.

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
# Alive if the worker touched the heartbeat less than this long ago. A step
# of a round waits a few times at most, and every wait has a limit, so a
# database that answers, however slowly, can't make the heartbeat late
# (app/worker.py). A late one means a stuck loop: a bug, or the one wait
# without a limit, a query to a PostgreSQL that is alive but stuck (app/db.py).
ALIVE_SECONDS: Final = 30.0


def is_alive(heartbeat: Path, now: Callable[[], float] = time.time) -> bool:
    """Has the worker gone round its loop recently, or through a step of a
    round? (Alive, not necessarily making progress: a worker waiting for the
    database to come back is alive.)"""
    try:
        return now() - heartbeat.stat().st_mtime < ALIVE_SECONDS
    except FileNotFoundError:
        return False


def main() -> int:
    return 0 if is_alive(HEARTBEAT) else 1


if __name__ == "__main__":
    sys.exit(main())
