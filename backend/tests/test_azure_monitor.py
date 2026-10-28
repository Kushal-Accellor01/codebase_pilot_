from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.contracts import Finding
from app.telemetry.azure_monitor import AzureMonitorLogSource, build_query
from app.telemetry.seeded import collect_evidence

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class StubClient:
    def __init__(self, rows, status="SUCCESS"):
        self.rows, self.status, self.calls = rows, status, []

    def query_workspace(self, workspace, kql, timespan):
        self.calls.append((workspace, kql, timespan))
        table = SimpleNamespace(
            columns=[SimpleNamespace(name="TimeGenerated"), SimpleNamespace(name="Log_s")], rows=self.rows
        )
        return SimpleNamespace(status=SimpleNamespace(name=self.status), tables=[table], partial_error="boom")


def source(rows, **kw):
    settings = Settings(_env_file=None, azure_log_analytics_workspace_id="ws-1")
    return AzureMonitorLogSource.from_settings(settings, "shop-api", client=StubClient(rows, **kw))


def test_query_text_and_validation():
    kql = build_query("ContainerAppConsoleLogs_CL", "Log_s", "ContainerAppName_s", "shop-api")
    assert "| where ContainerAppName_s == 'shop-api' and Log_s has 'deprecated'" in kql and kql.endswith("| take 5000")
    for bad in ("x'; drop table", "a b", "x\ny"):
        with pytest.raises(ValueError):
            build_query("T", "Log_s", "App_s", bad)
    with pytest.raises(ValueError):
        build_query("T; evil", "Log_s", "App_s", "ok")


def test_rows_become_log_lines_and_feed_the_same_evidence_code():
    rows = [
        (NOW - timedelta(hours=h), "express deprecated req.param(name): use req.params at routes/orders.js:8:25")
        for h in (1, 2, 30)
    ]
    src = source(rows)
    lines = list(src.query(None, NOW - timedelta(days=7), NOW))
    assert len(lines) == 3 and lines[0].service == "shop-api" and lines[0].ts.tzinfo is not None
    call = src._client.calls[0]
    assert call[0] == "ws-1" and call[2] == (NOW - timedelta(days=7), NOW)

    finding = Finding(
        file_path="routes/orders.js", line=8, snippet="", symbol="req.param", sources=["pattern"], confidence=0.8
    )
    evidence = collect_evidence(src, [finding], service="shop-api", now=NOW, source_name="azure_monitor")
    assert (
        evidence[0].count == 3 and evidence[0].source == "azure_monitor" and evidence[0].related_symbol == "req.param"
    )


def test_iso_strings_and_incomplete_queries():
    iso = source([("2026-10-28T10:00:00Z", "express deprecated x")])
    assert list(iso.query(None, NOW - timedelta(days=1), NOW))[0].ts.hour == 10
    with pytest.raises(RuntimeError, match="incomplete"):
        list(source([], status="PARTIAL_FAILURE").query(None, NOW - timedelta(days=1), NOW))
    with pytest.raises(ValueError):
        list(
            AzureMonitorLogSource("ws", table="T", message_col="M", app_col="A", client=StubClient([])).query(
                None, NOW, NOW
            )
        )
