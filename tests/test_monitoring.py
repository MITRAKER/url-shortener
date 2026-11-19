"""The monitoring setup agrees with the app (monitoring/, docs/RUNBOOK.md).

To Prometheus, a query for a metric that doesn't exist is not an error: it
just finds nothing. Rename a metric in app/metrics.py and forget the
dashboard, and a panel goes blank; forget an alert, and it never fires, all
without a word. So these tests read every query and check every name in it
against the metrics the app and the worker define. (promtool, in CI, checks
how the alerts behave: monitoring/prometheus/alerts_test.yml.)
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import redis
import yaml
from prometheus_client import CollectorRegistry
from prometheus_client.registry import Collector
from sqlalchemy import create_engine

from app.clicks import STREAM
from app.metrics import REGISTRY, WORKER_REGISTRY, BacklogCollector, DatabasePoolCollector

ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "monitoring/grafana/dashboards/url-shortener.json"
DATA_SOURCES = ROOT / "monitoring/grafana/provisioning/datasources/prometheus.yml"
ALERTS = ROOT / "monitoring/prometheus/alerts.yml"
ALERT_TESTS = ROOT / "monitoring/prometheus/alerts_test.yml"
RUNBOOK = ROOT / "docs/RUNBOOK.md"

# Words of PromQL itself that look like names.
PROMQL = frozenset(
    {
        "and",
        "or",
        "unless",
        "sum",
        "min",
        "max",
        "avg",
        "count",
        "rate",
        "increase",
        "histogram_quantile",
        "vector",
        "max_over_time",
    }
)
# Series that Prometheus makes itself, for every target.
BUILT_IN = frozenset({"up", "scrape_duration_seconds"})
# The series each type of metric produces.
SUFFIXES = {
    "counter": ("_total", "_created"),
    "gauge": ("",),
    "histogram": ("_bucket", "_count", "_sum", "_created"),
    "info": ("_info",),
}


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def series_of(collector: Collector) -> set[str]:
    return {
        family.name + suffix
        for family in collector.collect()
        for suffix in SUFFIXES.get(family.type, ("",))
    }


def metric_names(promql: str) -> set[str]:
    """The metric names in a query: what is left once strings, label
    matchers, ranges, label lists and PromQL's own words are taken out."""
    promql = re.sub(r'"[^"]*"', "", promql)
    promql = re.sub(r"\{[^}]*\}", "", promql)  # {job="app"}
    promql = re.sub(r"\[[^\]]*\]", "", promql)  # [5m]
    promql = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", "", promql)
    return set(re.findall(r"[A-Za-z_:][A-Za-z0-9_:]*", promql)) - PROMQL


def alert_rules() -> list[dict[str, Any]]:
    return [rule for group in load(ALERTS)["groups"] for rule in group["rules"] if "alert" in rule]


def dashboard_queries() -> list[str]:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    return [target["expr"] for panel in panels for target in panel.get("targets", [])]


@pytest.fixture
def defined(redis_client: redis.Redis, tmp_path: Path) -> set[str]:
    """Every series name there is: the app's, the worker's, those of the
    collectors registered at startup, the recording rules', Prometheus's own."""
    names = series_of(REGISTRY) | series_of(WORKER_REGISTRY) | BUILT_IN
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    collectors = CollectorRegistry()
    collectors.register(DatabasePoolCollector(engine, limit=30))
    collectors.register(BacklogCollector(redis_client, STREAM))
    names |= series_of(collectors)
    engine.dispose()
    rules = [rule for group in load(ALERTS)["groups"] for rule in group["rules"]]
    return names | {rule["record"] for rule in rules if "record" in rule}


def test_the_query_reader_finds_the_metric_names() -> None:
    query = (
        'sum by (instance_number) (rate(http_requests_total{job="app", route="/{code}"}[5m])'
        " * on (instance) group_left (instance_number) app_info) > (14.4 * 0.001)"
    )
    assert metric_names(query) == {"http_requests_total", "app_info"}


def test_the_dashboard_uses_only_metrics_that_exist(defined: set[str]) -> None:
    for query in dashboard_queries():
        assert metric_names(query) <= defined, query


def test_the_alerts_use_only_metrics_that_exist(defined: set[str]) -> None:
    for group in load(ALERTS)["groups"]:
        for rule in group["rules"]:
            assert metric_names(rule["expr"]) <= defined, rule["expr"]


def test_every_alert_links_to_its_section_of_the_runbook() -> None:
    headings = set(re.findall(r"^### (\w+)$", RUNBOOK.read_text(encoding="utf-8"), re.MULTILINE))
    for rule in alert_rules():
        name = rule["alert"]
        assert name in headings, f"docs/RUNBOOK.md has no section for {name}"
        assert rule["annotations"]["runbook_url"].endswith(f"/docs/RUNBOOK.md#{name.lower()}")


def test_every_alert_has_a_test_that_makes_it_fire() -> None:
    fired = {
        check["alertname"]
        for test in load(ALERT_TESTS)["tests"]
        for check in test["alert_rule_test"]
        if check["exp_alerts"]
    }
    assert {rule["alert"] for rule in alert_rules()} <= fired


def test_every_panel_asks_the_provisioned_prometheus() -> None:
    [source] = load(DATA_SOURCES)["datasources"]
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids)), "two panels share an id"
    for panel in panels:
        if panel["type"] != "row":
            assert panel["datasource"] == {"type": "prometheus", "uid": source["uid"]}
