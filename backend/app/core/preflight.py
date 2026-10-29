"""`python -m app.core.preflight [--repo URL]`: is this machine ready for a live run?

Every check says what it found and, when it fails, what to change. Nothing is written anywhere except a probe file
in the data folders; secrets are never printed. The checks that call out (model, GitHub, npm, OSV, Azure Monitor)
are the ones the offline tests cannot cover, so run this on the machine that will run the demo, with the real `.env`.
"""
import argparse
import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.updates.http import HttpClient

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
GITHUB_API = "https://api.github.com"
OSV_API = "https://api.osv.dev/v1/query"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _slug(entry: str) -> str | None:
    return entry if re.fullmatch(r"[\w.-]+/[\w.-]+", entry) else None


def check_tools() -> list[Check]:
    out = []
    for tool, minimum in (("git", None), ("node", 18), ("npm", None)):
        path = shutil.which(tool)
        if not path:
            out.append(Check(f"tool: {tool}", FAIL, "not found on PATH", f"install {tool} and reopen the terminal"))
            continue
        version = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.strip()
        major = re.search(r"(\d+)", version)
        if minimum and major and int(major.group(1)) < minimum:
            out.append(Check(f"tool: {tool}", FAIL, version, f"Node {minimum} or newer is needed"))
        else:
            out.append(Check(f"tool: {tool}", OK, version))
    return out


def check_directories(settings: Settings) -> list[Check]:
    out = []
    folders = (
        ("database folder", Path(settings.codebase_pilot_db).resolve().parent),
        ("workspace", Path(settings.codebase_pilot_workspace).resolve()),
        ("reports", Path(settings.reports_dir).resolve()),
    )
    for label, path in folders:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".pilot-write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            out.append(Check(f"writable: {label}", OK, str(path)))
        except OSError as exc:
            out.append(Check(f"writable: {label}", FAIL, f"{path}: {exc}", "use a folder you can write to"))
    return out


def check_settings(settings: Settings) -> list[Check]:
    out = []
    if settings.llm_provider == "openai_compatible":
        azure = {
            "LLM_BASE_URL": settings.llm_base_url,
            "LLM_API_KEY": settings.llm_api_key,
            "LLM_MODEL_STRONG": settings.llm_model_strong,
            "LLM_MODEL_FAST": settings.llm_model_fast,
        }
    else:
        azure = {
            "AZURE_OPENAI_ENDPOINT": settings.azure_openai_endpoint,
            "AZURE_OPENAI_DEPLOYMENT_STRONG": settings.azure_openai_deployment_strong,
            "AZURE_OPENAI_DEPLOYMENT_FAST": settings.azure_openai_deployment_fast,
            "AZURE_OPENAI_API_VERSION": settings.azure_openai_api_version,
        }
    missing = [k for k, v in azure.items() if not v]
    if settings.llm_mode == "replay":
        out.append(Check("model settings", OK, "LLM_MODE=replay: answers come from the recorded cache"))
    elif missing:
        out.append(Check("model settings", FAIL, "missing " + ", ".join(missing),
                         "fill them in backend/.env (deployment names, not model names)"))
    elif settings.llm_provider == "openai_compatible":
        out.append(Check("model settings", OK, f"{settings.llm_base_url}, models: "
                         f"{settings.llm_model_strong} / {settings.llm_model_fast}"))
    else:
        auth = "API key" if settings.azure_openai_api_key else "Entra ID (az login / managed identity)"
        out.append(Check("model settings", OK, f"{settings.azure_openai_endpoint}, auth: {auth}"))
    if settings.github_token:
        out.append(Check("GITHUB_TOKEN", OK, "set"))
    else:
        out.append(Check("GITHUB_TOKEN", WARN, "not set",
                         "without it GitHub is read anonymously (60 requests per hour) and no pull request is opened, "
                         "only a local branch"))
    allowed = settings.exec_allowed
    if allowed:
        out.append(Check("EXEC_ALLOWED_REPOS", OK, ", ".join(sorted(allowed))))
    else:
        out.append(Check("EXEC_ALLOWED_REPOS", FAIL, "empty",
                         "list the owner/repo values (or local paths) whose code may be installed and run; the "
                         "dashboard scan button and the fix step need it"))
    if settings.pilot_api_token:
        out.append(Check("API protection", OK, "PILOT_API_TOKEN set"))
    else:
        out.append(Check("API protection", WARN, "no PILOT_API_TOKEN",
                         "anyone who can reach the API can approve; fine on localhost only"))
    if settings.github_webhook_secret:
        out.append(Check("webhook", OK, "GITHUB_WEBHOOK_SECRET set"))
    else:
        out.append(Check("webhook", SKIP, "no webhook secret (re-scan by hand or with PILOT_RESCAN_MINUTES)"))
    return out


async def check_model(settings: Settings, http: HttpClient) -> list[Check]:
    from app.core.llm import LLM, LLMError, _Smoke

    if settings.llm_mode == "replay":
        return [Check("model call", SKIP, "replay mode")]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE llm_cache (key TEXT PRIMARY KEY, model TEXT NOT NULL, response JSON NOT NULL, "
                 "created_at TEXT NOT NULL DEFAULT '')")
    out = []
    llm = LLM(conn, http, settings)
    for tier in ("strong", "fast"):
        try:
            answer = await llm.chat(
                _Smoke, prompt_version="smoke", tier=tier,  # type: ignore[arg-type]
                messages=[{"role": "system", "content": "You answer in JSON."},
                          {"role": "user", "content": 'Set "answer" to the word "pong".'}],
            )
            out.append(Check(f"model call: {tier}", OK, f"answered {answer.answer!r}"))
        except LLMError as exc:
            out.append(Check(f"model call: {tier}", FAIL, str(exc)[:300],
                             "check the endpoint, the deployment name, the API version and the key or your `az login`"))
    return out


async def check_github(settings: Settings, http: HttpClient) -> list[Check]:
    if not settings.github_token:
        return [Check("GitHub token", SKIP, "no token")]
    headers = {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"}
    try:
        me = await http.get("api.github.com", f"{GITHUB_API}/user", headers=headers)
    except Exception as exc:
        return [Check("GitHub token", FAIL, f"{type(exc).__name__}: {exc}"[:200], "check the network or proxy")]
    if me.status_code == 401:
        return [Check("GitHub token", FAIL, "rejected (401)", "create a new token and put it in GITHUB_TOKEN")]
    login = me.json().get("login", "?") if me.status_code == 200 else "?"
    scopes = me.headers.get("x-oauth-scopes")  # classic tokens only; fine-grained tokens send none
    kind = f"scopes: {scopes or 'none'}" if scopes is not None else "fine-grained token"
    out = [Check("GitHub token", OK, f"valid for {login}, {kind}")]
    for entry in sorted(settings.exec_allowed):
        slug = _slug(entry)
        if not slug:
            out.append(Check(f"repo {entry}", SKIP, "a local path, not on GitHub"))
            continue
        resp = await http.get("api.github.com", f"{GITHUB_API}/repos/{slug}", headers=headers)
        if resp.status_code != 200:
            out.append(Check(f"repo {slug}", FAIL, f"HTTP {resp.status_code}: not found or not visible to the token",
                             "fix the name, or give the token access to this repository"))
            continue
        data = resp.json()
        can_write = bool((data.get("permissions") or {}).get("push"))
        detail = f"default branch {data.get('default_branch')}, " + ("can push" if can_write else "read only")
        if data.get("archived"):
            out.append(Check(f"repo {slug}", FAIL, "archived", "archived repositories cannot take pull requests"))
        elif not can_write:
            out.append(Check(f"repo {slug}", WARN, detail,
                             "pull requests need write access: Contents and Pull requests (read and write) and Issues "
                             "(write) for a fine-grained token, or the `repo` scope for a classic one"))
        else:
            out.append(Check(f"repo {slug}", OK, detail))
    rate = await http.get("api.github.com", f"{GITHUB_API}/rate_limit", headers=headers)
    if rate.status_code == 200:
        core = rate.json()["resources"]["core"]
        out.append(Check("GitHub rate limit", OK if core["remaining"] > 200 else WARN,
                         f"{core['remaining']} of {core['limit']} left"))
    return out


async def check_registry(settings: Settings, http: HttpClient) -> list[Check]:
    base = settings.npm_registry_url.rstrip("/")
    headers = {"Accept": "application/vnd.npm.install-v1+json"}
    if settings.npm_registry_token:
        headers["Authorization"] = f"Bearer {settings.npm_registry_token}"
    host = re.sub(r"^https?://", "", base).split("/")[0]
    try:
        resp = await http.get(host, f"{base}/express", headers=headers)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return [Check("npm registry", FAIL, detail, "check NPM_REGISTRY_URL and the network")]
    if resp.status_code == 200:
        return [Check("npm registry", OK, f"{base} answered for express")]
    hint = "set NPM_REGISTRY_TOKEN" if resp.status_code in (401, 403) else "check NPM_REGISTRY_URL"
    return [Check("npm registry", FAIL, f"{base} returned {resp.status_code}", hint)]


async def check_osv(http: HttpClient) -> list[Check]:
    try:
        body = {"package": {"name": "express", "ecosystem": "npm"}, "version": "4.17.1"}
        resp = await http.post("api.osv.dev", OSV_API, json=body)
    except Exception as exc:
        return [Check("OSV advisories", WARN, f"{type(exc).__name__}: {exc}"[:200], "security data will be missing")]
    if resp.status_code == 200:
        return [Check("OSV advisories", OK, "answered")]
    return [Check("OSV advisories", WARN, f"HTTP {resp.status_code}", "security data will be missing from the reports")]


def check_azure_monitor(settings: Settings) -> list[Check]:
    if not settings.azure_log_analytics_workspace_id:
        return [Check("Azure Monitor", SKIP, "no AZURE_LOG_ANALYTICS_WORKSPACE_ID (seeded logs are used)")]
    try:
        from datetime import datetime, timedelta, timezone

        from azure.identity import DefaultAzureCredential
        from azure.monitor.query import LogsQueryClient
    except ImportError:
        return [Check("Azure Monitor", FAIL, "azure-monitor-query / azure-identity not installed",
                      "pip install azure-monitor-query azure-identity")]
    try:
        client = LogsQueryClient(DefaultAzureCredential())
        end = datetime.now(timezone.utc)
        client.query_workspace(settings.azure_log_analytics_workspace_id, "print 1",
                               timespan=(end - timedelta(hours=1), end))
        return [Check("Azure Monitor", OK, "query succeeded (Log Analytics Reader works)")]
    except Exception as exc:
        return [Check("Azure Monitor", FAIL, f"{type(exc).__name__}: {exc}"[:240],
                      "run `az login`, or give this identity the Log Analytics Reader role on the workspace")]


def check_repo_access(url: str) -> list[Check]:
    """Can git read the repository Pilot is about to clone? Private repos need a credential helper or an SSH key."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    name = f"clone access: {url}"
    try:
        result = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True, timeout=60,
                                env=env)
    except subprocess.TimeoutExpired:
        return [Check(name, FAIL, "timed out", "check the network or the URL")]
    if result.returncode == 0:
        heads = [line.split("refs/heads/")[-1] for line in result.stdout.splitlines()]
        return [Check(name, OK, f"{len(heads)} branches, e.g. {', '.join(heads[:3])}")]
    lines = result.stderr.strip().splitlines()
    return [Check(name, FAIL, lines[-1][:200] if lines else "failed",
                  "for a private repository sign in with Git Credential Manager (`git ls-remote <url>` must work "
                  "without a prompt) or use an SSH URL; Pilot never passes your token to git")]


async def run_checks(settings: Settings, *, repo: str | None = None, model: bool = True,
                     http: HttpClient | None = None) -> list[Check]:
    results = check_tools() + check_directories(settings) + check_settings(settings)
    owns = http is None
    http = http or HttpClient()
    try:
        if model:
            results += await check_model(settings, http)
        results += await check_github(settings, http)
        results += await check_registry(settings, http)
        results += await check_osv(http)
    finally:
        if owns:
            await http.aclose()
    results += check_azure_monitor(settings)
    if repo:
        results += check_repo_access(repo)
        from app.core.workspace import repo_identity

        identity = repo_identity(repo) or repo
        allowed = {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
        if identity in allowed:
            results.append(Check("repo allowlist", OK, f"{identity} may be scanned and its code run"))
        else:
            results.append(Check("repo allowlist", FAIL, f"{identity} is not in EXEC_ALLOWED_REPOS",
                                 f"add {identity} to EXEC_ALLOWED_REPOS, or the dashboard refuses to scan it"))
    return results


def render(results: list[Check]) -> str:
    marks = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}
    lines = []
    for c in results:
        lines.append(f"{marks[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"         -> {c.fix}")
    failed = sum(c.status == FAIL for c in results)
    warned = sum(c.status == WARN for c in results)
    verdict = " Not ready." if failed else " Ready."
    lines += ["", f"{len(results)} checks: {failed} failed, {warned} warnings.{verdict}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.core.preflight", description=__doc__)
    parser.add_argument("--repo", help="a repository URL you intend to scan; checks git access and the allowlist")
    parser.add_argument("--no-model", action="store_true", help="skip the two model calls")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    results = asyncio.run(run_checks(get_settings(), repo=args.repo, model=not args.no_model))
    print(render(results))
    return 1 if any(c.status == FAIL for c in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
