import json
import logging

import respx

from app.core.logging import JsonFormatter, configure_logging
from app.updates import cli, store

from .test_imp5 import BASE, doc


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("w",), None)
    record.run_id = "r1"
    record.node = "impact"
    out = json.loads(JsonFormatter().format(record))
    assert out["message"] == "hello w" and out["run_id"] == "r1" and out["node"] == "impact"
    assert out["level"] == "INFO" and out["ts"].endswith("Z")


def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    configure_logging("INFO", json_format=True)
    pilot = [h for h in logging.getLogger().handlers if getattr(h, "_pilot", False)]
    assert len(pilot) == 1 and isinstance(pilot[0].formatter, JsonFormatter)
    configure_logging("WARNING", json_format=False)


@respx.mock
def test_verbose_logs_one_line_per_package_with_duration(patched_db, capsys, monkeypatch):
    monkeypatch.setenv("NPM_REGISTRY_URL", BASE)
    monkeypatch.delenv("NPM_REGISTRY_TOKEN", raising=False)
    store.migrate(store.connect(str(patched_db)))
    for name in ("supertest", "morgan", "eslint"):
        respx.get(f"{BASE}/{name}").respond(200, json=doc(name))
    assert cli.main(["registry", "--db", str(patched_db), "--verbose"]) == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "registry " in line and " ms)" in line]
    assert len(lines) == 3
    configure_logging("WARNING")


def test_quiet_by_default(patched_db, capsys):
    cli.main(["preflight", "--db", str(patched_db)])
    assert capsys.readouterr().err == ""
