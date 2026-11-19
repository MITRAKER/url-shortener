"""Is the stack watched? Prometheus collects the metrics of every app instance
and of the click worker, its rules work, every dashboard query runs, Grafana
serves the dashboard, and /metrics is hidden from outside.

CI runs it against the Docker stack, after the smoke test, so that there is
traffic to see. Only the standard library is needed.

Usage:
    python -m scripts.check_metrics http://localhost:8000 http://localhost:9090 \\
        http://localhost:3000
"""

import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.smoke_test import SmokeTestError, expect, request

DASHBOARD = Path(__file__).parent.parent / "monitoring/grafana/dashboards/url-shortener.json"
# Every target Prometheus should find, and how many of each.
TARGETS = {"app": 3, "click-worker": 1, "prometheus": 1}
# Prometheus scrapes and checks its rules every 15 s. Ask once a second, for a
# minute at most (bounded, NASA/JPL rule 2).
POLLS = 60
POLL_SECONDS = 1.0


def get_json(base_url: str, path: str) -> Any:
    status, _, body = request(base_url, "GET", path)
    expect(status == HTTPStatus.OK, f"GET {path} returned {status}: {body[:200]!r}")
    return json.loads(body)


def query(prometheus: str, promql: str) -> list[Any]:
    """The result of an instant query. Raises if Prometheus refuses it."""
    answer = get_json(prometheus, "/api/v1/query?" + urlencode({"query": promql}))
    expect(answer["status"] == "success", f"query {promql!r} failed: {answer}")
    return list(answer["data"]["result"])


def targets_missing(prometheus: str) -> str | None:
    """None once every target is up; otherwise, what isn't."""
    targets = get_json(prometheus, "/api/v1/targets?state=active")["data"]["activeTargets"]
    up = Counter(target["labels"]["job"] for target in targets if target["health"] == "up")
    missing = [f"{job}: {up[job]} of {n} up" for job, n in TARGETS.items() if up[job] != n]
    return "; ".join(missing) or None


def rules_failing(prometheus: str) -> str | None:
    """None once every rule has run without an error; otherwise, which hasn't."""
    groups = get_json(prometheus, "/api/v1/rules")["data"]["groups"]
    rules = [rule for group in groups for rule in group["rules"]]
    expect(bool(rules), "Prometheus has no rules: is alerts.yml loaded?")
    for rule in rules:  # bounded by the rules file
        if rule["health"] != "ok":
            return f"{rule['name']}: {rule['health']} {rule.get('lastError', '')}"
    return None


def requests_unseen(prometheus: str) -> str | None:
    """None once Prometheus has collected some request counts."""
    result = query(prometheus, 'sum(http_requests_total{job="app"})')
    if result and float(result[0]["value"][1]) > 0:
        return None
    return "no request counted yet"


def wait_for(what: str, problem: Callable[[], str | None]) -> None:
    """Ask `problem` until it answers None, or time is up."""
    found: str | None = "not checked"
    for _ in range(POLLS):  # bounded (NASA/JPL rule 2)
        found = problem()
        if found is None:
            return
        time.sleep(POLL_SECONDS)
    raise SmokeTestError(f"{what}: {found}")


def run_dashboard_queries(prometheus: str) -> int:
    """Run every query of the dashboard, as Grafana would. Returns how many."""
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panels = dashboard["panels"]
    queries = [target["expr"] for panel in panels for target in panel.get("targets", [])]
    for promql in queries:  # bounded by the dashboard
        # $__rate_interval is Grafana's: it picks a window from the zoom level.
        query(prometheus, promql.replace("$__rate_interval", "1m"))
    return len(queries)


def check_grafana(grafana: str) -> None:
    expect(get_json(grafana, "/api/health").get("database") == "ok", "Grafana is not healthy")
    # Anonymous visitors may look (docker-compose.yml): no login needed.
    found = get_json(grafana, "/api/dashboards/uid/url-shortener")["dashboard"]["title"]
    expect(found == "URL shortener", f"Grafana's dashboard is called {found!r}")


def check(app_url: str, prometheus: str, grafana: str) -> str:
    status, _, _ = request(app_url, "GET", "/metrics")
    expect(status == HTTPStatus.NOT_FOUND, f"/metrics through Nginx returned {status}, not 404")
    wait_for("Prometheus targets", lambda: targets_missing(prometheus))
    wait_for("Prometheus rules", lambda: rules_failing(prometheus))
    wait_for("Requests in Prometheus", lambda: requests_unseen(prometheus))
    queries = run_dashboard_queries(prometheus)
    check_grafana(grafana)
    return f"every target up, every rule fine, {queries} dashboard queries run, Grafana ready"


def main(argv: Sequence[str]) -> int:
    match argv:
        case [app_url, prometheus, grafana]:
            try:
                print(f"Metrics check passed: {check(app_url, prometheus, grafana)}")
            except (SmokeTestError, OSError, ValueError, KeyError) as exc:
                print(f"Metrics check FAILED: {exc}", file=sys.stderr)
                return 1
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
