# Codebase Pilot: Implementation Plan

Status as of Wed 28 Oct 2026, 12:15 IST (repo `Kushal-Accellor01/codebase_pilot_`, commit `ce5ee83`).
Demo: Friday 30 Oct. This file is the work queue for people and for Claude Code. **Update the Status column
of Section 8 when a ticket starts and finishes.**

Ticket format: **ID · lane · depends on** → what to build (files) → acceptance (commands + expected result).
Specs referenced: **BS** = `codebase-pilot-build-spec.md`, **S2** = `dependency-update-intelligence-spec.md`,
**DS** = `codebase-pilot-status-and-dashboard-spec.md`.

> Naming: the dashboard spec's API milestones A0–A2 are **API-0…API-2** here, and the build spec's GitHub
> actions milestone A1 is **ACT-1**, so no two tickets share an ID.

---

## 1. Where we are

| Area | State | Evidence |
| --- | --- | --- |
| Step 1 ingestion | Works (original code) | `backend/app/*.py`; patch not applied |
| Step 2 M0 store, migration, preflight, inventory | Done | `updates/store.py`, `preflight.py`, `cli.py`, `test_m0.py` |
| Step 2 M1 npm registry, HTTP client, cache | Done | `updates/registry/*`, `http.py`, `test_m1.py`, recorded fixtures |
| Step 2 M2 version resolution | Done | `updates/versions.py`, `test_m2.py` |
| Step 2 M3–M8 | Not started | `dependency_updates` never written |
| Core, Steps 3–8, graph, worker | Not started | — |
| API, dashboard | Not started | Mockup ready: `docs/design/dashboard-mockup.html` |
| Sandbox repo, Azure, CI | Not started | — |

Review of the implemented code: clean structure, single SQL module, network outside transactions,
per-package failure isolation, and tests for both ingestion schema shapes. No functional bugs found.
Production gaps are listed as Part A tickets (pinning, CI, private-registry auth, concurrency, logging,
config and migrations consolidation).

---

## 2. How the original plan maps to what we build

| Original plan section | What happens to it now | Where |
| --- | --- | --- |
| 1 Scope lock (OpenAPI contract change in `payments-service`) | **Changed:** demo change source is the Express 4 → 5 upgrade in `pilot-sandbox-shop`; the loop is the same. The OpenAPI scenario becomes stretch ticket OAS-1 | BS 1, 11; Part F |
| 2 Architecture (sources → ingestion → knowledge → agents → API/UI) | Kept; two-level orchestration (scan pipeline + one LangGraph run per upgrade) | BS 3 |
| 3 Stack (Postgres + pgvector, FastAPI, React, LangGraph, Langfuse) | SQLite now behind store modules; Postgres in production; rest kept | BS 2, 14 |
| 4 Setup (IT asks, env, CI, board) | Tickets IMP-1…3, Z0, C0 | Parts A, B |
| 5, 22 Team split | Lanes in Section 6 | Section 6 |
| 6 Day-by-day plan | Rebased on today's state | Section 6 |
| 7 Shared contracts | `core/contracts.py` + Step 2's `DependencyChangeEvent` + API types | BS 6, S2 9, DS 3.3 |
| 8 Working agreements, risks, demo script | Ground rules in `CLAUDE.md`; demo script | BS 16 |
| 9 Feature additions (pre-merge guard, per-team notices, deadline countdown) | Stretch: B-1 (bot PRs) covers "comment on the PR"; others in Part F | Part F |
| 10 Low-level guide (graph, LLM layer, API, fixes) | Kept, updated for Azure OpenAI and library upgrades | BS 7 |
| 11 Quality gates | Part E checklist + CI + evals | Part E |
| 12 Dashboard or plugin | Dashboard + GitHub PR now; bot-PR comments as stretch | DS 4; BS 7.0B |
| 13–16 Integration, rollout, production, seams | Production design, Azure mapping | BS 2A, 3.3, 14 |
| 17–21 The team's nine steps | 1–3 ingestion + Step 2; 4 = Step 2 M3–M6; 5 = I-1/I-2; 6 = approval in G-1 + dashboard; 7 = ACT-1; 8–9 = re-scan + Step 2 diff (U-M7) | S2, BS 7 |

---

## 3. Part A: improve what's implemented

| ID · lane · depends | What to build | Acceptance |
| --- | --- | --- |
| **IMP-1** · Step 2 · — | `backend/pyproject.toml`: add `[project]` with `name = "codebase-pilot"`, `requires-python = ">=3.11"`. Pin exact versions in `requirements.txt` and `requirements-dev.txt` (from the team's working venv: `pip freeze`, keep only direct dependencies) | Fresh venv: `pip install -r requirements-dev.txt && pytest -q` green on Windows and Linux |
| **IMP-2** · Step 2 · IMP-1 | `.github/workflows/ci.yml`: job `backend` (ubuntu, Python 3.11, cache pip, `ruff check .`, `pytest -q`, working directory `backend`). Add a `frontend` job later in D-0 | CI green on a PR |
| **IMP-3** · Step 2 · — | `.gitignore`: add `.env`, `*.db-wal`, `*.db-shm`, `workspace/`, `checkpoints.db`, `frontend/node_modules/`, `frontend/dist/`, `reports/`. Add `backend/.env.example` with every variable in `CLAUDE.md` (empty values, one-line comments) | `git status` clean after a full local run |
| **IMP-4** · Step 2 · — | Private registry support: `NPM_REGISTRY_TOKEN` → `Authorization: Bearer <token>` **only** on requests to the `NPM_REGISTRY_URL` host. HTTP 401/403 → `save_registry_failure` with `"registry rejected credentials (HTTP 401)"`, no retries. Never log the header | respx tests: header present for the registry host only; 401 recorded as error; token absent from captured logs |
| **IMP-5** · Step 2 · — | `updates/registry/batch.py`: `fetch_many(adapter, conn, names, refresh)` runs fetches concurrently (`asyncio.gather` over tasks; the per-host semaphore limits them); each result saved as it completes; exceptions turned into failure rows. Use it in `cli registry`, `cli versions` and the pipeline. npm host read timeout 60 s (large packuments) | Test with 20 mocked packages where one returns 500 ×3: 19 saved, 1 error row, completes in < 2 s with `wait_scale=0` |
| **IMP-6** · Step 2 · — | `app/core/logging.py`: `configure_logging(level, json: bool)` (JSON lines when `LOG_FORMAT=json`). CLIs call it; library code uses `logging.getLogger(__name__)`; add `--verbose` to the CLI | `cli versions --verbose` logs one line per package with duration; no `print` outside `cli.py` |
| **IMP-7** · Core · C-0 | Move env reading to `app/core/config.py` (pydantic-settings). Keep `updates/config.py` as thin wrappers so existing imports keep working | All existing tests green unchanged |
| **IMP-8** · Core · C-0 | Migration runner in `core/db.py`: applies `updates/migrations/002_*.sql` then `core/migrations/003_*.sql`, records each in `schema_migrations`; `updates.store.migrate` delegates to it | Applying twice is a no-op; `schema_migrations` lists 002 and 003 |
| **IMP-9** · Ingestion · — | Ingestion owner applies `ingestion-fixes.patch` (`git apply --check` first), commits, re-scans the sandbox and Express | `cli inventory` on Express shows 28 runtime / 16 dev; `pytest -q` still green |

---

## 4. Part B: build tickets, in dependency order

### 4.1 Today (Wednesday)

| ID · lane · depends | What to build | Acceptance |
| --- | --- | --- |
| **S-1** · Sandbox · — | Repo `pilot-sandbox-shop` per BS 11: Express **4.21.2** (exact pin), mocha + supertest, `package-lock.json`, routes with the planted APIs (`req.param`, `res.json(obj, 201)`, `res.sendfile`, `app.del`, `res.send(204)`, `req.acceptsLanguage`, `app.get('*')`), `lib/notify.js` negative control, seeded logs `logs/shop-api.jsonl`, `CODEOWNERS` (`* @<org>/team-shop`), `evals/sandbox-shop.answer_key.yaml` with real line numbers | `npm ci && npm test` green on 4.21.2; stderr contains a `depd` warning for each planted API marked "depd" in BS 11 (drop rows that don't warn); on `express@5` (`npm i express@5 --no-save`) at least 4 tests fail |
| **Z-0** · Azure · — | Azure OpenAI: two deployments (strong, fast), both structured-output capable; keys to the team via a shared secret store (not chat); `infra/README.md` with the exact `az` commands used | `python -m app.core.llm --smoke` (after C-1) returns structured answers from both; until then a curl in `infra/README.md` returns 200 |
| **U-M7L** · Step 2 · M2 | `updates/pipeline.py` + `cli run --no-llm [--only]`: for the latest scan, inventory → `fetch_many` → `resolve_versions` → priority (S2 5.5) → risk by rules without notes (S2 5.10 rows that don't need changes) → upsert `dependency_updates` (status `up_to_date`, `pending`, `not_in_registry`, `unsupported_ecosystem`, `invalid_declaration`, `error`) → `pipeline_runs` row with stats. Non-npm ecosystems → `unsupported_ecosystem` | On the Express DB: 44 rows, every row has a status, `pipeline_runs.status` is `succeeded` or `partial`; one package raising doesn't stop the run; a second run updates the same 44 rows |
| **C-0** · Core · — | `core/config.py`, `core/db.py` (connect + migration runner), `core/migrations/003_pipeline.sql` (BS 5.1, including `validations.tests_passed/tests_total`), `core/contracts.py` (BS 6), `tests/fixtures/golden/` with one `DependencyChangeEvent` and one full upgrade run (JSON) | Migrations apply twice on patched and unpatched DBs; fixtures validate against the models |
| **API-0** · Core · C-0, U-M7L | `api/main.py` (FastAPI, CORS for `localhost:5173`, `/api` prefix), `api/queries.py`, routes: `GET /api/health`, `/api/scans`, `/api/scans/{id or latest}/dependencies`, `/api/dependency-updates/{id}` (DS 3.2–3.3) | `pytest` with a temp DB: shapes match the DS types; `uvicorn` serves the Express DB's 44 rows |
| **D-0** · Dashboard · — | `frontend/` scaffold per DS 6: Vite + React + TS strict, router, TanStack Query, `tokens.css` (DS 5.1), Rail + TopBar, API client with `VITE_API_MODE=fixtures\|live`, fixtures from the mockup's `data` object, Vitest; CI job `frontend` (`npm ci`, `npm run build`, `npm test`) | Shell matches the mockup in light and dark; CI green |
| **D-1** · Dashboard · D-0 | Overview page (DS 4.2): headline, upgrade board, right column, affected files, loading/empty/error states | Matches the mockup at 1440 px and 390 px; no horizontal scroll at 360 px |

### 4.2 Thursday

| ID · lane · depends | What to build | Acceptance |
| --- | --- | --- |
| **U-M3** · Step 2 · U-M7L | OSV security check (S2 5.4): `updates/security.py`, `vulnerabilities`, `dependency_update_vulns`, `min_safe_version` | respx tests: batch shape, ordering, pagination, fixed-version calculation |
| **U-M4** · Step 2 · U-M3 | Source resolution chain + verification (S2 5.6), `package_sources`, override YAML | Each chain level tested; mismatched repo name rejected |
| **U-M5** · Step 2 · U-M4 | Release notes (S2 5.7): GitHub releases, changelog files (both heading styles), monorepo tags, compare fallback, truncation; `release_notes` | Fixture tests for each path, including Express's `History.md` |
| **U-M6** · Step 2 · U-M5, C-1 | LLM extraction (S2 5.8) via `core.llm.chat`, evidence validation (S2 5.9), risk (S2 5.10), `update_analyses`, `update_changes` | Fake LLM with one valid and one invented quote → one kept, one in `dropped_json`; second run makes 0 LLM calls |
| **U-M7** · Step 2 · U-M6 | Full `cli run` (with LLM) + `report --format md\|json`; emit `DependencyChangeEvent`s; enqueue `upgrade_run` jobs for risk ≥ medium (once G-1 exists; until then write the events JSON) | On the sandbox scan: `express` event with ≥ 5 validated breaking changes, each with a quote found in the fetched notes |
| **C-1** · Core · C-0, Z-0 | `core/llm.py` (Azure OpenAI, cache, record/replay, `--smoke`), move `HttpClient` to `core/http.py` (re-export from `updates/http.py`), `core/events.py`, `core/jobs.py` (claim with `RETURNING`, retry with backoff, crash recovery), `core/workspace.py` (worktree add/remove, safe `run()` with timeouts, allowlist, stripped env) | Unit tests: job retried with backoff; stuck job recovered; command killed at timeout; non-allowlisted repo refused; token not in child env |
| **ING-2** · Ingestion · IMP-9 | `orchestrator/pipeline.py: ingest(repo_url) -> scan_id` calling the same functions as `main.py`, returning the id | Test: scanning a tiny local git repo returns a scan id with nodes |
| **G-1** · Core · C-1, ING-2 | `orchestrator/graph.py` (BS 7.9) with stub nodes, `SqliteSaver` on `checkpoints.db`, `@traced` writing `run_events` and `upgrade_runs.status`; `orchestrator/worker.py` (loop: claim → run → complete; `resume_upgrade` jobs); `orchestrator/cli.py`: `scan <url>`, `run-upgrades --scan latest [--until-idle]` (process queued jobs, exit when none are runnable), `worker` (loop forever), `reset-demo` | Pause at approval, resume via `Command(resume=...)`, retry loop (max 3 attempts), `report_only` route: all tested |
| **I-1** · Impact · S-1, C-1 | `impact/scope.py` (BS 7.1 step 2 query), `impact/patterns.py` (step 3), `impact/probes.py` runtime-deprecation probe + `parse_deprecations` (step 4) | On the sandbox: every answer-key row found by at least one source; `lib/notify.js` never listed |
| **I-2** · Impact · I-1 | Upgrade-and-test probe (step 5), merge and score (6), LLM confirmation with `tier="fast"` (7), persist (8); `telemetry/seeded.py` (BS 7.2) | `evals/run_eval.py`: recall 1.0, precision ≥ 0.9; telemetry matches `req.param` and `res.sendfile` |
| **PL-1** · Remediation · I-2 | `planning/risk.py`, `effort.py`, `plan.py` (BS 7.3), `remediation_plans` | Rules unit-tested for every row of the severity table; effort formula tested |
| **R-1** · Remediation · PL-1 | `remediation/codemods.py` (only if the Express codemod package is confirmed), `edits.py` (BS 7.5 rules), `validate.py` (manifest bump, `npm test`, parse `N passing`/`M failing` into `tests_passed/tests_total`), retry loop | Sandbox: tests red on 5.x without edits, green with them; a rule-breaking edit lands in `rejected_edits` |
| **ACT-1** · Remediation · R-1 | `actions/github.py` (BS 7.8): branch, contents API commits, PR with evidence body, labels, idempotent; issue for `needs_engineer` | respx tests: second call returns the same PR; body contains evidence links. Live: one real PR on the sandbox |
| **API-1** · Core · C-0 | `GET /api/scans/{id}/overview`, `/api/upgrades`, `/api/upgrades/{run_id}` with the DS 3.4 queries and 3.5 sentence templates | Template tests for every row (singular, plural, empty); overview of the sandbox matches the data |
| **D-2** · Dashboard · D-1, API-0 | Dependencies table with filters + Dependency detail (DS 4.1) on the live API | Filters work on fixtures and live data |
| **D-3** · Dashboard · D-1, API-1 | Upgrade detail: header, evidence strip, six tabs (DS 4.3) | Each tab renders its fixture and its empty state |

### 4.3 Thursday evening → Friday noon

| ID · lane · depends | What to build | Acceptance |
| --- | --- | --- |
| **API-2** · Core · G-1 | `POST /api/scans`, `POST /api/upgrades/{id}/decision` (409 if not awaiting), SSE `GET /api/upgrades/{id}/events` (replay from `Last-Event-ID`, then poll every 500 ms), `POST /api/feedback` | Decision resumes the graph; SSE replays after reconnect |
| **D-4** · Dashboard · D-3, API-2 | Approvals page, Approve/Reject dialogs, live Activity via SSE | Approve on the live API moves the run to `pr_opened`; Overview updates without reload |
| **E2E-1** · All · everything above | Golden path test in replay mode (`pytest -m e2e`), record the LLM cache on one live run (`LLM_MODE=record`) | Section 5 script passes end to end in under 5 minutes, offline |
| **D-5** · Dashboard · D-4 | Accessibility pass, Lighthouse ≥ 95 (Overview, Upgrade detail), Playwright smoke test of the golden path | Smoke test green in CI |
| **REH-1** · All · E2E-1 | Three rehearsals with the demo script (BS 16), backup video, `make demo-reset` equivalent: `orchestrator/cli.py reset-demo` (delete Pilot branches and PRs on the sandbox, truncate run tables, re-scan) | Three clean runs in a row |

---

## 5. Part C: end-to-end acceptance script (the definition of "works")

Run from a clean checkout on the demo machine. Every step lists what must be true.

```bash
cd backend && source .venv/bin/activate        # Windows: .venv\Scripts\activate
export CODEBASE_PILOT_DB=demo.db LLM_MODE=replay EXEC_ALLOWED_REPOS=<org>/pilot-sandbox-shop

# 1. Scan
python -m app.orchestrator.cli scan https://github.com/<org>/pilot-sandbox-shop
#    → scan id printed; nodes > 0; DECLARES has dependency_type (patch applied)

# 2. Dependency updates
python -m app.updates.cli run
#    → express: current 4.21.2, latest 5.x, update_type major, status analyzed,
#      >= 5 update_changes rows, every evidence_quote found in release_notes.body_md
#    → pipeline_runs.status = succeeded; 0 LLM calls in replay mode beyond cache hits

# 3. Upgrade runs (worker processes the queued jobs, then stops at approval)
python -m app.orchestrator.cli run-upgrades --scan latest --until-idle
#    → upgrade_runs: express = awaiting_approval
#    → impact_findings match the answer key: python ../evals/run_eval.py --db demo.db
#      (recall 1.0, precision >= 0.9)
#    → validations: attempt without fix fails, final attempt passes (tests_passed == tests_total)
#    → telemetry_evidence has rows for req.param and res.sendfile

# 4. Dashboard
uvicorn app.api.main:app --port 8000 &
(cd ../frontend && VITE_API_MODE=live npm run dev)
#    → Overview headline names express; board shows Critical + Awaiting approval
#    → Upgrade detail: findings, quotes, plan, diff, before/after tests, activity

# 5. Approve (UI button, or:)
curl -X POST localhost:8000/api/upgrades/<run_id>/decision -H 'content-type: application/json' \
     -d '{"decision":"approved","comment":"demo"}'
#    → upgrade_runs: pr_opened; pull_requests row with url (live mode) or mocked (replay)

# 6. Re-run safety
python -m app.updates.cli run && python -m app.orchestrator.cli run-upgrades --scan latest --until-idle
#    → no duplicate rows, no new PR, 0 new LLM calls
```

---

## 6. Owners and schedule

| Lane | Owner | Wed (today) | Thu | Fri until noon |
| --- | --- | --- | --- | --- |
| Ingestion + Azure | ingestion owner | IMP-9, Z-0 | ING-2, then help G-1 or Z-1 | REH-1 |
| Step 2 | Kushal | IMP-1…6, U-M7L | U-M3 → U-M7 | E2E-1 fixes, record cache |
| Impact | P3 | I-1 scaffolding on S-1 as soon as it lands | I-1, I-2 | Eval numbers on the slide |
| Sandbox + remediation | P4 | **S-1 first (by early afternoon)** | PL-1, R-1, ACT-1 | B-1 only if E2E-1 is green |
| Core + API | P5 | C-0, API-0 | C-1, G-1, API-1, API-2 | E2E-1 |
| Dashboard | P6 | D-0, D-1 | D-2, D-3, D-4 | D-5, deck |

**Gates.** Wed 18:00: S-1, C-0, U-M7L, API-0 merged and the dashboard shows real Express rows.
Thu 18:00: golden path runs live once end to end; LLM cache recorded. Fri 12:00: feature freeze.

**Cut order if behind:** stretch tickets → D-5 Lighthouse target → telemetry (seeded only) → LLM
confirmation in I-2 (keep pattern + runtime + test sources) → codemod (LLM edits only) → live PR (show the
prepared diff and body). Never cut: evidence quotes, the upgrade-and-test probe, the approval step, the
Overview board.

---

## 7. Part E: production-readiness checklist

| Area | Requirement | Ticket |
| --- | --- | --- |
| Reproducible builds | Pinned Python and npm dependencies; Python 3.11+ declared | IMP-1, D-0 |
| CI | Lint, tests, frontend build, eval thresholds on every PR | IMP-2, D-0, E2E-1 |
| Secrets | Env only; `.env.example`; tokens never logged or passed to child processes | IMP-3, IMP-4, C-1 |
| Safe execution | Allowlist, `--ignore-scripts`, timeouts, stripped env; containers in production | C-1, BS 7.6 |
| Data integrity | Migrations recorded; idempotent upserts; unique keys on runs, findings, PRs | IMP-8, C-0 |
| Failure isolation | Per-package and per-run error status; job retries with backoff; crash recovery | U-M7L, C-1 |
| LLM grounding | Quote validation, path/edit validation, schema-only output, prompt versions, cache | U-M6, I-2, R-1 |
| Observability | Structured logs with run ids; `run_events`; `/api/health`; App Insights on Azure | IMP-6, C-1, API-0, Z-1 |
| Accessibility | Keyboard focus, ARIA tabs/dialog, text alongside colour, no horizontal scroll | D-1…D-5 |
| Evaluation | Answer key, recall/precision thresholds, quote-validation rate | I-2, E2E-1 |
| Demo safety | Replay mode, demo reset, backup video | E2E-1, REH-1 |

---

## 8. Part F: stretch tickets (only after E2E-1 is green)

| ID | What | Spec |
| --- | --- | --- |
| **B-1** | Trigger B: comment on Dependabot/Renovate bump PRs, optional fix commit | BS 7.0B |
| **Z-1** | Deploy API + worker to Azure Container Apps, UI to Static Web Apps, App Insights | BS 3.3 |
| **T-2** | Sandbox deployed to Container Apps; telemetry from real Log Analytics | BS 7.2 |
| **OAS-1** | Original plan's Use Case 2: `ChangeEvent(source='openapi')` from an `oasdiff` of a provider spec; impact = consumer HTTP-call search; same upgrade graph from impact onward | Plan doc 1, 10 |
| **FEAT-1** | Per-team migration notices (one issue per owning team from `CODEOWNERS`) | Plan doc 9 |
| **FEAT-2** | Deadline countdown in the board (deprecation or end-of-life date when known) | Plan doc 9 |
| **PY-1** | PyPI adapter for Step 2 (same interface as npm) | S2 5.2 |

---

## 9. Ticket board (update as you go)

| ID | Lane | Status | Owner | Notes |
| --- | --- | --- | --- | --- |
| IMP-1 | Step 2 | Not started | Kushal | |
| IMP-2 | Step 2 | Not started | Kushal | |
| IMP-3 | Step 2 | Not started | Kushal | |
| IMP-4 | Step 2 | Not started | Kushal | |
| IMP-5 | Step 2 | Not started | Kushal | |
| IMP-6 | Step 2 | Not started | Kushal | |
| IMP-7 | Core | Not started | P5 | after C-0 |
| IMP-8 | Core | Not started | P5 | after C-0 |
| IMP-9 | Ingestion | Not started | ingestion owner | |
| S-1 | Sandbox | Not started | P4 | blocks I-1, R-1 |
| Z-0 | Azure | Not started | ingestion owner | blocks C-1 smoke |
| U-M7L | Step 2 | Not started | Kushal | blocks API-0 data |
| C-0 | Core | Not started | P5 | |
| API-0 | Core | Not started | P5 | |
| D-0 | Dashboard | Not started | P6 | |
| D-1 | Dashboard | Not started | P6 | |
| U-M3 | Step 2 | Not started | Kushal | |
| U-M4 | Step 2 | Not started | Kushal | |
| U-M5 | Step 2 | Not started | Kushal | |
| U-M6 | Step 2 | Not started | Kushal | needs C-1 |
| U-M7 | Step 2 | Not started | Kushal | |
| C-1 | Core | Not started | P5 | |
| ING-2 | Ingestion | Not started | ingestion owner | |
| G-1 | Core | Not started | P5 | |
| I-1 | Impact | Not started | P3 | |
| I-2 | Impact | Not started | P3 | |
| PL-1 | Remediation | Not started | P4 | |
| R-1 | Remediation | Not started | P4 | |
| ACT-1 | Remediation | Not started | P4 | |
| API-1 | Core | Not started | P5 | |
| API-2 | Core | Not started | P5 | |
| D-2 | Dashboard | Not started | P6 | |
| D-3 | Dashboard | Not started | P6 | |
| D-4 | Dashboard | Not started | P6 | |
| D-5 | Dashboard | Not started | P6 | |
| E2E-1 | All | Not started | P5 | |
| REH-1 | All | Not started | all | |

---

## 10. Prompts for Claude Code

> **Kushal, now:** Read `CLAUDE.md` and `docs/specs/implementation-plan.md`. Do IMP-1, IMP-3, IMP-4, IMP-5 and
> IMP-6 in that order, one commit each, then U-M7L. After each ticket run `ruff check .` and `pytest -q`,
> update the ticket board, and show me the output. Stop after U-M7L.

> **P5, now:** Read `CLAUDE.md`, the implementation plan and BS Sections 5–6. Do C-0, then API-0. Stop after
> each and show the test output.

> **P6, now:** Read `CLAUDE.md`, DS Sections 3–6 and open `docs/design/dashboard-mockup.html`. Do D-0, then D-1
> in fixture mode. Show screenshots at 1440 px and 390 px.

> **P4, now:** Read `CLAUDE.md` and BS Section 11. Do S-1 in a new folder `pilot-sandbox-shop/`. Show the test
> output and the captured `depd` warnings on 4.21.2, and the failures on Express 5. Stop before pushing.
