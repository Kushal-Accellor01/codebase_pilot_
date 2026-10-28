"""Shared pieces of the offline golden path: the sandbox mirror, recorded GitHub/npm data, a scripted model and
a fake test runner that replays the output captured from the real sandbox."""
import json
import re
from pathlib import Path

import httpx
import respx

from app.core import workspace
from app.core.config import Settings
from app.core.llm import LLM
from app.impact.confirm import Verdict, Verdicts
from app.planning.plan import PlanText
from app.remediation.fix import FixEdits
from app.updates.models import ChangeItem, SummaryOut, UpdateAnalysisOut
from app.updates.registry.base import PackageInfo, VersionInfo
from tests.remediation.test_remediation import FIXES
from tests.updates.test_umt7l import FakeRegistry, info

FIX = Path(__file__).resolve().parents[1] / "fixtures"
SANDBOX_ABS = r"C:\Projects\codebase_pilot_\pilot-sandbox-shop"
GH = "https://api.github.com"
EXPRESS_VERSIONS = (
    ["4.21.2", "4.22.0", "4.22.1", "4.22.2"]
    + [f"5.0.0-alpha.{n}" for n in range(1, 9)] + [f"5.0.0-beta.{n}" for n in range(1, 4)]
    + ["5.0.0", "5.0.1", "5.1.0", "5.2.0", "5.2.1", "5.3.0"]
)


def registry() -> FakeRegistry:
    express = PackageInfo(
        ecosystem="npm", name="express", latest_version="5.3.0", repository="expressjs/express",
        versions=[VersionInfo(version=v, published_at="2020-01-01T00:00:00.000Z",
                              engines={"node": ">= 0.10" if v.startswith("4") else ">= 18"})
                  for v in EXPRESS_VERSIONS],
    )
    return FakeRegistry({"express": express, "mocha": info("mocha", ["10.8.2"]), "supertest": info("supertest", ["6.3.4"])})


def mock_github() -> None:
    """Source verification, release notes (recorded Express releases + History.md); everything else is 404."""
    releases = json.loads((FIX / "notes" / "express_releases.json").read_text(encoding="utf-8"))
    history = (FIX / "notes" / "express_History.md").read_text(encoding="utf-8")
    respx.get(f"{GH}/repos/expressjs/express/contents/package.json").respond(200, text=json.dumps({"name": "express"}))
    respx.get(f"{GH}/repos/expressjs/express").respond(
        200, json={"name": "express", "owner": {"login": "expressjs"}, "default_branch": "master", "archived": False})
    respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=releases)
    respx.get(f"{GH}/repos/expressjs/express/contents/History.md").respond(200, text=history)
    respx.get(url__regex=rf"{GH}/repos/expressjs/express/(contents/.*|compare/.*)").respond(404)


def breaking_changes() -> list[ChangeItem]:
    """What a good model extracts from the recorded Express notes (quotes are verbatim lines of History.md)."""
    def change(symbols, quote, version, description, kind="removed"):
        return ChangeItem(kind=kind, breaking=True, api_symbols=symbols, description=description, version=version,
                          evidence_quote=quote, migration_hint=None)

    return [
        change(["req.param"], "`req.param()` -- use `req.params`, `req.body`, or `req.query` instead",
               "5.0.0-alpha.2", "req.param() was removed"),
        change(["app.del"], "`app.del` - use `app.delete`", "5.0.0-alpha.1", "app.del was removed"),
        change(["req.acceptsLanguage"], "`req.acceptsLanguage` - use `req.acceptsLanguages`", "5.0.0-alpha.1",
               "req.acceptsLanguage was removed"),
        change(["res.json(obj, status)"], "`res.json(obj, status)` signature - use `res.json(status, obj)`",
               "5.0.0-alpha.1", "the res.json(obj, status) signature was removed", kind="signature_changed"),
        change([], "Updated to `path-to-regexp@8.x`, removing sub-expression regex patterns", "5.0.0",
               "Route path syntax changed: path-to-regexp 8 needs named wildcards", kind="behavior_changed"),
    ]


class ScriptedLLM(LLM):
    """Answers each schema the way a good model would; counts calls like the real gateway."""

    def __init__(self):
        super().__init__(None, None, Settings(_env_file=None, azure_openai_deployment_strong="scripted"))
        self.by_schema: dict[str, int] = {}

    async def chat(self, schema, *, prompt_version, messages, tier="strong"):
        name = schema.__name__
        self.calls += 1
        self.by_schema[name] = self.by_schema.get(name, 0) + 1
        user = messages[-1]["content"]
        if name == "UpdateAnalysisOut":
            return UpdateAnalysisOut(summary="Express 5 removes several APIs.", changes=breaking_changes(),
                                     notes_quality="detailed")
        if name == "SummaryOut":
            return SummaryOut(summary="Express 5 removes several APIs.")
        if name == "Verdicts":
            ids = [int(i) for i in re.findall(r"\[item (\d+)\]", user)]
            return Verdicts(results=[Verdict(id=i, affected=True, reason="uses the removed API") for i in ids])
        if name == "PlanText":
            return PlanText(rationale="The new version breaks tests and production already warns.",
                            steps=["Upgrade express.", "Replace the removed APIs."], validation=["Tests pass."])
        if name == "FixEdits":
            return FixEdits(edits=FIXES)
        raise AssertionError(f"unexpected schema {name}")


BASELINE = (FIX / "sandbox" / "baseline_4.21.2.txt").read_text(encoding="utf-8")
EXPRESS5 = (FIX / "sandbox" / "express5.txt").read_text(encoding="utf-8")


def install_fake_runner(monkeypatch, calls: list[str] | None = None) -> None:
    """Replace npm with a replay of the output captured from the real sandbox. The result depends on the
    worktree: not bumped -> baseline; bumped but code unfixed -> Express 5 failures; bumped and fixed -> green."""
    log = calls if calls is not None else []

    def result(args, exit_code, output):
        return workspace.CommandResult(" ".join(args), exit_code, 5, "\n".join(output.splitlines()[-200:]), False, output)

    def npm_ci(worktree, repo):
        workspace.assert_exec_allowed(repo)
        log.append("npm ci")
        return result(["npm", "ci"], 0, "added 100 packages")

    def npm_install_exact(worktree, repo, package, version):
        workspace.assert_exec_allowed(repo)
        log.append(f"npm install {package}@{version}")
        wt = Path(worktree)
        (wt / ".bumped").write_text(version, encoding="utf-8")
        manifest = wt / "package.json"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace('"express": "4.21.2"', f'"express": "{version}"'),
                            encoding="utf-8")
        return result(["npm", "install"], 0, "ok")

    def run_tests(worktree, repo, args=None):
        workspace.assert_exec_allowed(repo)
        wt = Path(worktree)
        fixed = "req.param(" not in (wt / "routes" / "orders.js").read_text(encoding="utf-8")
        if not (wt / ".bumped").exists():
            log.append("tests on 4.21.2")
            return result(["npm", "test"], 0, BASELINE)
        if not fixed:
            log.append("tests on 5.x without fix")
            return result(["npm", "test"], 1, EXPRESS5.replace(SANDBOX_ABS, str(wt)))
        log.append("tests on 5.x with fix")
        return result(["npm", "test"], 0, "  9 passing (120ms)\n")

    monkeypatch.setattr(workspace, "npm_ci", npm_ci)
    monkeypatch.setattr(workspace, "npm_install_exact", npm_install_exact)
    monkeypatch.setattr(workspace, "run_tests", run_tests)


def stub_http_transport() -> None:
    respx.route().mock(return_value=httpx.Response(404))  # anything not mocked explicitly fails loudly as 404
