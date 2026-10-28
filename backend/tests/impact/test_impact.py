import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import db as core_db
from app.core.config import Settings
from app.core.contracts import Finding
from app.core.llm import LLM
from app.impact import probes
from app.impact.confirm import Verdict, Verdicts, confirm_findings, needs_confirmation
from app.impact.merge import merge_findings
from app.impact.patterns import min_args, normalise_symbol, search_scope
from app.telemetry.seeded import SeededLogSource, collect_evidence, signature
from app.updates.models import ChangeRef
from tests.updates.conftest import make_db

ROOT = Path(__file__).resolve().parents[3]
SANDBOX = ROOT / "pilot-sandbox-shop"
FIX = Path(__file__).parent.parent / "fixtures" / "sandbox"
sys.path.insert(0, str(ROOT / "evals"))
from run_eval import evaluate, load_key  # noqa: E402

SOURCE_FILES = [
    "app.js", "routes/orders.js", "routes/invoices.js", "routes/admin.js", "routes/i18n.js",
    "routes/fallback.js", "lib/notify.js",
]


def change(cid, symbols, description="x", hint=None, kind="removed") -> ChangeRef:
    return ChangeRef(id=cid, kind=kind, breaking=True, api_symbols=symbols, description=description,
                     migration_hint=hint, version="5.0.0", evidence_url="u", evidence_quote="q")


CHANGES = [
    change(1, ["req.param"], "req.param(name) removed"),
    change(2, ["res.json(obj, status)"], "res.json(obj, status) signature removed"),
    change(3, ["res.sendfile"], "res.sendfile removed"),
    change(4, ["app.del"], "app.del removed"),
    change(5, ["res.send"], "res.send(status) number body no longer a status"),
    change(6, ["req.acceptsLanguage"], "req.acceptsLanguage removed"),
    change(7, [], "Path route matching syntax changed: unnamed wildcards need a name", kind="behavior_changed"),
]


# --- patterns -------------------------------------------------------------------------------------------


def test_normalise_symbol():
    assert normalise_symbol("req.param(name)") == "req.param"
    assert normalise_symbol("`res.json()`") == "res.json"
    assert normalise_symbol("  app.del  ") == "app.del"
    assert min_args("res.json(obj, status)") == 2 and min_args("req.param(name)") == 1 and min_args("res.send") == 0


def test_pattern_search_finds_every_planted_line_and_not_the_negative_control():
    hits = search_scope(SANDBOX, SOURCE_FILES, CHANGES)
    got = {(h.file_path, h.line) for h in hits}
    key = load_key()
    expected = {(f["file"], f["line"]) for f in key["expected_findings"]}
    assert expected <= got
    assert not any(h.file_path == "lib/notify.js" for h in hits)
    json_calls = [h for h in hits if h.symbol == "res.json"]
    assert [(h.file_path, h.line) for h in json_calls] == [("routes/orders.js", 18)]  # res.json(order) is fine


def test_confidence_exact_receiver_vs_any_receiver(tmp_path):
    (tmp_path / "a.js").write_text("req.param('x');\nrequest.param('y');\n", encoding="utf-8")
    hits = {h.line: h.confidence for h in search_scope(tmp_path, ["a.js"], [change(1, ["req.param"])])}
    assert hits == {1: 0.8, 2: 0.6}


def test_option_keys_and_comments_and_skips(tmp_path):
    (tmp_path / "a.js").write_text(
        "// bodyParser.urlencoded({ extended: true })\nbodyParser.urlencoded({ extended: true });\n", encoding="utf-8"
    )
    (tmp_path / "big.min.js").write_text("x.extended: 1", encoding="utf-8")
    hits = search_scope(tmp_path, ["a.js", "big.min.js", "missing.js"], [change(1, ["options.extended"])])
    assert [(h.line, h.confidence) for h in hits] == [(2, 0.4)]


def test_route_wildcard_only_when_a_change_is_about_path_syntax():
    with_change = search_scope(SANDBOX, ["routes/fallback.js"], CHANGES)
    assert [(h.line, h.symbol, h.confidence) for h in with_change] == [(2, "path-syntax", 0.6)]
    without = search_scope(SANDBOX, ["routes/fallback.js"], CHANGES[:6])
    assert without == []


# --- probes (real captured output) ---------------------------------------------------------------------------------


BASELINE = (FIX / "baseline_4.21.2.txt").read_text(encoding="utf-8")
EXPRESS5 = (FIX / "express5.txt").read_text(encoding="utf-8")
SANDBOX_ABS = r"C:\Projects\codebase_pilot_\pilot-sandbox-shop"


def test_parse_deprecations_from_real_output():
    found = probes.parse_deprecations(BASELINE, SANDBOX_ABS)
    got = {(d.file_path, d.line, d.symbol) for d in found}
    assert got == {
        ("routes/admin.js", 9, "app.del"), ("routes/admin.js", 5, "res.send"),
        ("routes/i18n.js", 3, "req.acceptsLanguage"), ("routes/invoices.js", 11, "res.sendfile"),
        ("routes/orders.js", 8, "req.param"), ("routes/orders.js", 18, "res.json"),
    }
    assert all(d.package == "express" for d in found)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Wed, 28 Oct 2026 05:12:00 GMT express deprecated req.param(name): Use req.params instead at routes/orders.js:12:15",
         ("routes/orders.js", 12, 15)),
        ("express deprecated res.sendfile: Use res.sendFile instead at routes/invoices.js:9:7", ("routes/invoices.js", 9, 7)),
        ("express deprecated app.del: Use app.delete instead routes/admin.js:7:3", ("routes/admin.js", 7, 3)),
        ("body-parser deprecated undefined extended: provide extended option at /repo/app.js:4:20", ("/repo/app.js", 4, 20)),
    ],
)
def test_parse_deprecations_formats(line, expected):
    d = probes.parse_deprecations(line, "/nowhere")[0]
    assert (d.file_path, d.line, d.column) == expected


def test_parse_deprecations_relative_to_root_and_ignores_node_modules():
    text = (
        "express deprecated res.sendfile: x at /repo/routes/a.js:3:1\n"
        "express deprecated req.param: y at /repo/node_modules/lib/b.js:3:1\n"
        "express deprecated res.sendfile: x at /repo/routes/a.js:3:1\n"
    )
    found = probes.parse_deprecations(text, "/repo")
    assert [(d.file_path, d.line) for d in found] == [("routes/a.js", 3)]


def test_failure_frames_from_real_express5_output():
    frames = {(f.file_path, f.line) for f in probes.parse_failure_frames(EXPRESS5, SANDBOX_ABS)}
    assert {("routes/orders.js", 8), ("routes/invoices.js", 11), ("routes/i18n.js", 3),
            ("routes/admin.js", 9), ("routes/fallback.js", 2)} <= frames
    assert not any("test" in p or "node_modules" in p for p, _ in frames)


def test_baseline_has_no_failure_frames():
    assert probes.parse_failure_frames(BASELINE, SANDBOX_ABS) == []


# --- merge and the answer key ------------------------------------------------------------------------------------------


def all_findings():
    hits = search_scope(SANDBOX, SOURCE_FILES, CHANGES)
    deps = probes.parse_deprecations(BASELINE, SANDBOX_ABS)
    frames = probes.parse_failure_frames(EXPRESS5, SANDBOX_ABS)
    return merge_findings(SANDBOX, hits, deps, frames, CHANGES)


def test_merge_collects_sources_and_scores():
    by = {(f.file_path, f.line): f for f in all_findings()}
    order = by[("routes/orders.js", 8)]
    assert order.symbol == "req.param" and order.change_id == 1
    assert set(order.sources) == {"pattern", "runtime_deprecation", "test_failure"}
    assert order.confidence == 1.0  # max 1.0 + 0.2 bonus, capped
    assert "req.param('id')" in order.snippet
    fallback = by[("routes/fallback.js", 2)]
    assert set(fallback.sources) == {"pattern", "test_failure"} and fallback.confidence == 1.0
    assert by[("routes/admin.js", 5)].change_id == 5


def test_eval_recall_one_precision_and_negative_control():
    result = evaluate(all_findings(), load_key())
    assert result["recall"] == 1.0, result["missed"]
    assert result["precision"] >= 0.9, result["extra"]
    assert result["negatives_reported"] == []


def test_confidence_bonus_for_two_sources():
    f = merge_findings(Path("."), [], probes.parse_deprecations(
        "express deprecated res.sendfile: x at a.js:1:1", "/r"), [probes.Frame("a.js", 1)], [change(3, ["res.sendfile"])])
    assert f[0].confidence == 1.0
    only_pattern = merge_findings(SANDBOX, search_scope(SANDBOX, ["routes/invoices.js"], [change(3, ["res.sendfile"])]),
                                  [], [], [change(3, ["res.sendfile"])])
    assert only_pattern[0].confidence == 0.8 and needs_confirmation(only_pattern[0])


# --- LLM confirmation ---------------------------------------------------------------------------------------------------------


class FakeLLM(LLM):
    def __init__(self, conn, answers):
        super().__init__(conn, None, Settings(_env_file=None))
        self.answers, self.prompts, self.tiers = list(answers), [], []

    async def chat(self, schema, *, prompt_version, messages, tier="strong"):
        self.prompts.append(messages[-1]["content"])
        self.tiers.append(tier)
        self.calls += 1
        return self.answers.pop(0)


@pytest.fixture
def conn(tmp_path):
    c = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(c)
    return c


def pattern_only():
    hits = search_scope(SANDBOX, SOURCE_FILES, CHANGES)
    return merge_findings(SANDBOX, hits, [], [], CHANGES)


def run(coro):
    return asyncio.run(coro)


def test_confirmation_marks_verdicts_ignores_invented_ids_and_uses_fast_tier(conn):
    findings = pattern_only()
    n = len(findings)
    answers = Verdicts(results=[Verdict(id=0, affected=True, reason="real"), Verdict(id=1, affected=False, reason="no"),
                                Verdict(id=999, affected=True, reason="invented")])
    llm = FakeLLM(conn, [answers] + [Verdicts(results=[])] * 3)
    calls = run(confirm_findings(llm, findings, SANDBOX, CHANGES, package="express", current="4.21.2", target="5.3.0"))
    assert calls == -(-n // 8) and set(llm.tiers) == {"fast"}
    assert findings[0].confirmed is True and findings[0].reason == "real"
    assert findings[1].confirmed is False
    assert all(f.confirmed is None for f in findings[2:])  # skipped ids stay unconfirmed
    assert "Package: express (4.21.2 -> 5.3.0)" in llm.prompts[0] and "[item 0]" in llm.prompts[0]
    assert ">>" in llm.prompts[0]  # the flagged line is marked inside its code window


def test_findings_with_runtime_evidence_skip_confirmation(conn):
    findings = all_findings()
    llm = FakeLLM(conn, [])
    run(confirm_findings(llm, findings, SANDBOX, CHANGES, package="express", current="4.21.2", target="5.3.0"))
    assert llm.calls == 0  # every planted line already has a runtime or test signal


def test_llm_failure_leaves_findings_unconfirmed(conn):
    from app.core.llm import LLMOutputError

    class Boom(FakeLLM):
        async def chat(self, *a, **k):
            raise LLMOutputError("nope")

    findings = pattern_only()
    run(confirm_findings(Boom(conn, []), findings, SANDBOX, CHANGES, package="p", current="1", target="2"))
    assert all(f.confirmed is None for f in findings)


def test_persisting_findings_roundtrip(conn):
    from app.orchestrator import store as run_store

    scan = "scan-new"
    conn.execute("INSERT INTO dependency_updates (scan_id, dep_node_id, ecosystem, name) VALUES (?, 'dep:npm:x', 'npm', 'x')", (scan,))
    conn.commit()
    update_id = conn.execute("SELECT id FROM dependency_updates").fetchone()["id"]
    run_id = run_store.create_run(conn, scan_id=scan, dependency_update_id=update_id, ecosystem="npm", package="x",
                                  from_version="1.0.0", to_version="2.0.0", risk_level="high")
    assert run_store.create_run(conn, scan_id=scan, dependency_update_id=update_id, ecosystem="npm", package="x",
                                from_version="1.0.0", to_version="2.0.0", risk_level="high") == run_id
    findings = all_findings()
    for f in findings:
        f.change_id = None  # no update_changes rows exist in this fixture
    findings[0].confirmed = False
    run_store.save_findings(conn, run_id, findings)
    run_store.save_findings(conn, run_id, findings)  # idempotent
    loaded = run_store.load_findings(conn, run_id)
    assert len(loaded) == len(findings) and loaded[0].confirmed is False
    assert len(run_store.load_findings(conn, run_id, include_rejected=False)) == len(findings) - 1
    assert loaded[0].sources == findings[0].sources


# --- telemetry -------------------------------------------------------------------------------------------------------------------


def test_signature_normalises_numbers_ids_and_timestamps():
    a = signature("Wed, 28 Oct 2026 05:12:00 GMT express deprecated req.param(name) at routes/orders.js:12:15")
    b = signature("Thu, 29 Oct 2026 09:01:44 GMT express deprecated req.param(name) at routes/orders.js:12:15")
    assert a == b and "<n>" in a
    assert signature("order 8f3a9c1d2e4b5a6c failed") == "order <id> failed"


def test_telemetry_matches_req_param_and_res_sendfile_in_seeded_logs():
    source = SeededLogSource(SANDBOX / "logs")
    evidence = collect_evidence(source, all_findings())
    by_symbol = {e.related_symbol: e for e in evidence}
    assert {"req.param", "res.json", "res.sendfile"} <= set(by_symbol)
    assert by_symbol["req.param"].count > 5 and by_symbol["req.param"].source == "seeded_logs"
    assert by_symbol["req.param"].first_seen < by_symbol["req.param"].last_seen
    assert "req.param" in by_symbol["req.param"].sample
    total = sum(1 for line in (SANDBOX / "logs" / "shop-api.jsonl").read_text().splitlines() if "deprecated" in line)
    assert sum(e.count for e in evidence) <= total


def test_telemetry_window_and_no_findings(tmp_path):
    log = tmp_path / "x.jsonl"
    log.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "service": "s", "level": "warn", "msg": "express deprecated req.param(name)"}) + "\n"
        + json.dumps({"ts": "2026-10-28T00:00:00Z", "service": "s", "level": "warn", "msg": "express deprecated req.param(name)"}) + "\nnot json\n",
        encoding="utf-8",
    )
    finding = Finding(file_path="a.js", line=1, snippet="", symbol="req.param", sources=["pattern"], confidence=0.8)
    evidence = collect_evidence(SeededLogSource(tmp_path), [finding])
    assert [e.count for e in evidence] == [1]  # the January line is outside the 7-day window
    assert collect_evidence(SeededLogSource(tmp_path), []) == []
    assert collect_evidence(SeededLogSource(tmp_path), [finding], now=datetime(2026, 1, 2, tzinfo=timezone.utc))[0].count == 1
