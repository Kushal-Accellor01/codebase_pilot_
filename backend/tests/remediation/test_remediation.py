import asyncio
import shutil
from pathlib import Path

import pytest

from app.core import db as core_db
from app.core import workspace
from app.core.config import Settings
from app.core.contracts import DependencyChangeEvent, Edit, Finding
from app.core.llm import LLM, LLMOutputError
from app.remediation import codemods
from app.remediation.edits import MAX_CHANGED_LINES, MAX_EDITS, apply_edits, is_test_file
from app.remediation.fix import FixEdits, make_patch, remaining_findings
from app.updates.models import ChangeRef
from tests.conftest import make_sandbox_repo
from tests.updates.conftest import make_db

SCOPE = ["app.js", "routes/orders.js", "routes/invoices.js", "routes/admin.js", "routes/i18n.js", "routes/fallback.js"]


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_sandbox_repo(tmp_path / "work")


def edit(file, find, replace) -> Edit:
    return Edit(file=file, find=find, replace=replace)


FIXES = [
    edit("routes/orders.js", "req.param('id')", "req.params.id"),
    edit("routes/orders.js", "res.json(order, 201)", "res.status(201).json(order)"),
    edit("routes/invoices.js", "res.sendfile(", "res.sendFile("),
    edit("routes/admin.js", "app.del(", "app.delete("),
    edit("routes/admin.js", "res.send(204)", "res.sendStatus(204)"),
    edit("routes/i18n.js", "req.acceptsLanguage('en')", "req.acceptsLanguages('en')"),
    edit("routes/fallback.js", "app.get('*'", "app.get('/*splat'"),
]


# --- edit rules -------------------------------------------------------------------------------------------


def test_valid_edits_apply_and_pass_the_syntax_check(repo):
    result = apply_edits(repo, FIXES, SCOPE)
    assert len(result.applied) == 7 and result.rejected == []
    assert "req.params.id" in (repo / "routes/orders.js").read_text(encoding="utf-8")
    assert "app.delete(" in (repo / "routes/admin.js").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad,reason",
    [
        (edit("../outside.js", "a", "b"), "outside the worktree"),
        (edit("/etc/passwd", "a", "b"), "outside the worktree"),
        (edit("C:/Windows/x.js", "a", "b"), "outside the worktree"),
        (edit("package.json", '"express"', '"hax"'), "manifests and lockfiles"),
        (edit("package-lock.json", "a", "b"), "manifests and lockfiles"),
        (edit("lib/notify.js", "param(", "p("), "not in the package scope"),
        (edit("routes/orders.js", "", "x"), "empty find"),
        (edit("routes/orders.js", "does not occur", "x"), "occurs 0 times"),
        (edit("routes/orders.js", "res.", "x."), "occurs"),  # several matches
    ],
)
def test_rule_violations_are_rejected_and_nothing_is_written(repo, bad, reason):
    before = {p: p.read_text(encoding="utf-8") for p in repo.rglob("*.js") if ".git" not in p.parts}
    result = apply_edits(repo, [bad], SCOPE)
    assert result.applied == [] and reason in result.rejected[0]["reason"]
    assert before == {p: p.read_text(encoding="utf-8") for p in before}


def test_missing_file_inside_the_scope_is_rejected(repo):
    result = apply_edits(repo, [edit("routes/missing.js", "a", "b")], [*SCOPE, "routes/missing.js"])
    assert "does not exist" in result.rejected[0]["reason"]


def test_test_files_may_be_edited_even_outside_the_scope(repo):
    assert is_test_file("test/orders.test.js") and is_test_file("src/__tests__/a.js") and not is_test_file("routes/a.js")
    result = apply_edits(repo, [edit("test/orders.test.js", "returns an order by id", "returns an order")], SCOPE)
    assert len(result.applied) == 1


def test_syntax_error_reverts_all_edits_to_that_file(repo):
    good = edit("routes/orders.js", "req.param('id')", "req.params.id")
    broken = edit("routes/orders.js", "const orders = new Map(", "const orders = new Map((((")
    other = edit("routes/invoices.js", "res.sendfile(", "res.sendFile(")
    original = (repo / "routes/orders.js").read_text(encoding="utf-8")
    result = apply_edits(repo, [good, broken, other], SCOPE)
    assert (repo / "routes/orders.js").read_text(encoding="utf-8") == original
    assert [e.file for e in result.applied] == ["routes/invoices.js"]
    assert len(result.rejected) == 2 and all("syntax error" in r["reason"] for r in result.rejected)


def test_caps_on_edit_count_and_changed_lines(repo):
    many = [edit("routes/orders.js", "req.param('id')", "req.params.id")] + [
        edit("routes/orders.js", f"zzz{i}", "y") for i in range(MAX_EDITS)
    ]
    result = apply_edits(repo, many, SCOPE, syntax_check=False)
    assert len(result.applied) == 1 and any("more than 30" in r["reason"] for r in result.rejected)

    big = "\n".join("x" for _ in range(MAX_CHANGED_LINES))
    (repo / "routes" / "orders.js").write_text("// start\n" + big + "\nTAIL\n", encoding="utf-8")
    result = apply_edits(repo, [edit("routes/orders.js", big, "y"), edit("routes/orders.js", "TAIL", "a\nb")], SCOPE,
                         syntax_check=False)
    assert len(result.applied) == 1 and "200 changed lines" in result.rejected[0]["reason"]


# --- patch loop with a fake model -----------------------------------------------------------------------------


class FakeLLM(LLM):
    def __init__(self, conn, answers=None, error=None):
        super().__init__(conn, None, Settings(_env_file=None))
        self.answers, self.error, self.prompts = list(answers or []), error, []

    async def chat(self, schema, *, prompt_version, messages, tier="strong"):
        self.prompts.append(messages[-1]["content"])
        if self.error:
            raise self.error
        return self.answers.pop(0)


@pytest.fixture
def conn(tmp_path):
    c = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(c)
    return c


def event() -> DependencyChangeEvent:
    return DependencyChangeEvent(
        event_id="e", scan_id="s", ecosystem="npm", package="express", dependency_type="runtime",
        manifest_path="package.json", current_version="4.21.2", current_version_basis="exact_pin",
        target_version="5.3.0", update_type="major", requires_manifest_change=True, runtime_requirement_change=False,
        is_deprecated=False, importer_count=4, priority_score=60.0, risk_level="high", status="analyzed",
        changes=[ChangeRef(id=1, kind="removed", breaking=True, api_symbols=["req.param"], description="req.param removed",
                           migration_hint="Use req.params", version="5.0.0", evidence_url="u", evidence_quote="Remove req.param")],
    )


def findings_for(repo: Path) -> list[Finding]:
    def line_of(file, text):
        return next(i for i, ln in enumerate((repo / file).read_text(encoding="utf-8").splitlines(), 1) if text in ln)

    return [
        Finding(file_path="routes/orders.js", line=line_of("routes/orders.js", "req.param('id')"),
                snippet="const id = Number(req.param('id'));", symbol="req.param", change_id=1,
                sources=["pattern"], confidence=0.8),
        Finding(file_path="routes/admin.js", line=line_of("routes/admin.js", "app.del("),
                snippet="app.del('/admin/cache', clearCache);", symbol="app.del", sources=["pattern"], confidence=0.8),
        Finding(file_path="routes/i18n.js", line=3, snippet="gone", symbol="x", sources=["pattern"], confidence=0.8,
                confirmed=False),
    ]


def run(coro):
    return asyncio.run(coro)


def test_remaining_findings_skip_rejected_and_already_fixed(repo):
    findings = findings_for(repo)
    assert [f.file_path for f in remaining_findings(repo, findings)] == ["routes/orders.js", "routes/admin.js"]
    apply_edits(repo, FIXES[:1], SCOPE)
    assert [f.file_path for f in remaining_findings(repo, findings)] == ["routes/admin.js"]


def test_patch_diff_comes_from_git_and_prompt_holds_the_evidence(repo, conn):
    llm = FakeLLM(conn, [FixEdits(edits=[FIXES[0], FIXES[3]])])
    patch = run(make_patch(llm, event(), repo, None, findings_for(repo), SCOPE, attempt=1, bump=False))
    assert patch.method == "llm_edits" and len(patch.edits) == 2 and patch.rejected == []
    assert "-  const id = Number(req.param('id'));" in patch.diff and "+  const id = Number(req.params.id);" in patch.diff
    assert "+  app.delete('/admin/cache', clearCache);" in patch.diff
    assert patch.manifest_change is None and patch.validation is None
    prompt = llm.prompts[0]
    assert "req.param removed" in prompt and "Remove req.param" in prompt and ">>" in prompt
    assert "routes/i18n.js" not in prompt  # the rejected finding is not sent


def test_retry_prompt_carries_the_failing_test_output(repo, conn):
    llm = FakeLLM(conn, [FixEdits(edits=[])])
    run(make_patch(llm, event(), repo, None, findings_for(repo), SCOPE, attempt=2, bump=False,
                   failure_tail="1) orders returns an order\n  AssertionError: expected 200"))
    assert "AssertionError: expected 200" in llm.prompts[0] and "previous attempt failed" in llm.prompts[0]


def test_rejected_edits_are_recorded_not_applied(repo, conn):
    bad = edit("package.json", '"express": "4.21.2"', '"express": "9.9.9"')
    llm = FakeLLM(conn, [FixEdits(edits=[FIXES[0], bad])])
    patch = run(make_patch(llm, event(), repo, None, findings_for(repo), SCOPE, attempt=1, bump=False))
    assert len(patch.edits) == 1 and patch.rejected[0]["file"] == "package.json"
    assert '"express": "4.21.2"' in (repo / "package.json").read_text(encoding="utf-8")


def test_model_error_gives_an_empty_patch_with_a_reason(repo, conn):
    llm = FakeLLM(conn, error=LLMOutputError("bad"))
    patch = run(make_patch(llm, event(), repo, None, findings_for(repo), SCOPE, attempt=1, bump=False))
    assert patch.edits == [] and "model error" in patch.rejected[0]["reason"] and patch.diff == ""


def test_no_findings_left_means_no_model_call(repo, conn):
    llm = FakeLLM(conn, [])
    patch = run(make_patch(llm, event(), repo, None, [], SCOPE, attempt=1, bump=False))
    assert patch.edits == [] and llm.prompts == []


def test_manifest_bump_requires_an_allowlisted_repo(repo, conn):
    with pytest.raises(workspace.ExecNotAllowed):
        run(make_patch(None, event(), repo, "evil/repo", [], SCOPE, attempt=1, bump=True))


def test_manifest_change_is_reported_from_git(repo, conn, monkeypatch):
    monkeypatch.setattr("app.remediation.fix.bump_manifest", lambda *a: _fake_bump(repo))
    patch = run(make_patch(None, event(), repo, "any", [], SCOPE, attempt=1, bump=True))
    assert patch.manifest_change and '+    "express": "5.3.0"' in patch.manifest_change
    assert "package.json" in patch.diff


def _fake_bump(repo: Path):
    path = repo / "package.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"express": "4.21.2"', '"express": "5.3.0"'), encoding="utf-8")
    return True, ""


def test_no_codemod_is_registered_until_verified():
    assert codemods.CODEMODS == {} and codemods.find_codemod("express", "4.21.2", "5.3.0") is None
    codemods.CODEMODS["demo"] = [codemods.Codemod("demo", 1, 2, ("echo", "hi"))]
    try:
        assert codemods.find_codemod("demo", "1.4.0", "2.0.0") is not None
        assert codemods.find_codemod("demo", "1.4.0", "3.0.0") is None
        assert codemods.find_codemod("demo", "x", "2.0.0") is None
    finally:
        codemods.CODEMODS.clear()


def test_sandbox_repo_fixture_is_a_clean_git_repo(repo):
    assert workspace.diff(repo) == "" and shutil.which("node")
