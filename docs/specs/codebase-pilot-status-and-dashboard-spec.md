# Codebase Pilot: Status, Critical Path and Dashboard Build Spec

> **For Claude Code.** Read this file first, then the two specs it builds on. Files and where they go:
>
> | File | Purpose | Location in the repo |
> | --- | --- | --- |
> | `codebase-pilot-status-and-dashboard-spec.md` (this file) | What exists, what's missing, the order to build it, and the full dashboard spec | `docs/specs/` |
> | `codebase-pilot-build-spec.md` | Architecture, contracts, Steps 3–8, Azure, API | `docs/specs/` |
> | `dependency-update-intelligence-spec.md` | Step 2 in full (milestones M0–M8) | `docs/specs/` |
> | `dashboard-mockup.html` | **Visual reference** for the dashboard (open it in a browser) | `docs/design/` |
> | `ingestion-fixes.patch` | Tested fixes to ingestion, applied by its owner | repo root |
>
> Rules: build only the milestone you are asked for; each ends with its tests passing; never write to
> the ingestion tables; all SQL lives in store/query modules; the UI must match the mockup's tokens,
> layout and copy rules (Section 5).

---

## 1. What exists today (repo `Kushal-Accellor01/codebase_pilot_`, commit `ce5ee83`)

| Area | Status | Notes |
| --- | --- | --- |
| Ingestion (`backend/app/*.py`) | **Works, original version** | Same as the ingestion owner's repo plus a UTF-8 console fix in `main.py`. **`ingestion-fixes.patch` is not applied**, so: re-scans overwrite earlier scans' nodes, `dependency_type` is missing, imports have no line numbers, relative imports can resolve to the wrong folder |
| Step 2 M0: store, migration 002, preflight, inventory CLI | **Done** | `updates/store.py`, `preflight.py`, `cli.py` (`migrate`, `preflight`, `inventory`); migration identical to the spec |
| Step 2 M1: npm registry adapter + HTTP client + cache | **Done** | Retries, `Retry-After`, per-host limits, ETag/TTL cache, scoped names, `NPM_REGISTRY_URL` |
| Step 2 M2: version resolution | **Done** | All spec cases plus extras (scan-date cut-off, lockfile, pre-releases, engines) |
| Tests | **About 70 tests** across `test_m0.py`, `test_m1.py`, `test_m2.py`, with recorded npm fixtures | Not run in this review (packages couldn't be installed here); run `pytest -q` from `backend/` |
| Step 2 M3–M8: OSV, sources, release notes, LLM analysis, evidence, risk, pipeline `run`, report | **Not started** | `dependency_updates` is never written yet, so nothing downstream has data |
| Core (`app/core`: config, db, migrations 003, LLM gateway, jobs, events, workspace) | **Not started** | |
| Steps 3–8 (impact, telemetry, plan, fix, validate, approval, PR) and the LangGraph graph | **Not started** | |
| API (FastAPI) | **Not started** | `fastapi`/`uvicorn` listed in requirements, no app |
| Frontend | **Not started** | |
| Sandbox repo `pilot-sandbox-shop` | **Not started** | Everything after Step 2 needs it |
| Azure (Azure OpenAI deployments, Container Apps) | **Not started** | Access granted |

### 1.1 Code review notes on what exists

The Step 2 code is clean, typed, and follows the spec closely (single SQL module, network calls outside
transactions, one package's failure never stops the run). Small items to fix when convenient:

1. `requirements.txt` uses `>=` ranges; pin exact versions (`pip freeze` into a lock or use `uv`) before the
   demo so every machine gets the same packages.
2. `registry_versions` is upserted but never pruned; versions unpublished from npm stay in the cache.
   Harmless now; note it in a comment.
3. `cli.py` has `registry` and `versions` but no `run`; M7 adds `run`, which writes `dependency_updates`
   (the table everything downstream and the dashboard read).
4. `app/init.py` is empty and misnamed (`__init__.py`); imports work today because `app` is treated as a
   namespace package. Leave it for the ingestion owner.
5. Apply `ingestion-fixes.patch` (the ingestion owner), then re-scan: the Step 2 tests already cover both
   patched and unpatched databases.

---

## 2. What "functional" means by the demo

One repository (the sandbox) goes through the whole loop, and every step is visible in the dashboard:

1. **Scan** → dependencies listed with current and latest versions *(dashboard: Dependencies)*.
2. **Updates analysed** → risk, advisories, breaking changes with quotes *(Overview board, What changed)*.
3. **Impact** → affected lines with evidence sources *(Affected code)*.
4. **Production evidence** → seeded or Azure logs matched *(Production evidence)*.
5. **Plan, fix, tests** → plan, diff, before/after test results *(Plan, Fix and tests)*.
6. **Approve** → PR opened *(Approvals, Activity)*.

### 2.1 Critical path (do these in this order; parallel lanes in Section 7)

| # | Work | Spec | Why it's on the critical path |
| --- | --- | --- | --- |
| 1 | Apply ingestion patch; build the sandbox repo; Azure OpenAI deployments + keys | build spec 11, 3.3 (Z0) | Everything downstream needs correct scans, a target with real breaking changes, and an LLM |
| 2 | **Step 2 M7-lite:** `cli run --no-llm` writes `dependency_updates` (versions, deprecation, priority, risk without notes) | Step 2 spec 5.5, 5.10, 5.11 | First real data for the dashboard; unblocks the API |
| 3 | **Read API A0** over existing tables + **dashboard D0–D2** against it | this file, 4 and 6 | Judges see a working product early; UI work stops depending on the backend |
| 4 | Step 2 M3 (OSV), M4 (sources), M5 (notes), M6 (LLM extraction + evidence), full M7 | Step 2 spec | "What changed" with real quotes |
| 5 | Core C0/C1 + graph G1 (stubs) + worker | build spec 7.9, 7.10 | The upgrade run and the approval pause |
| 6 | Impact I1/I2, plan, remediation R1, actions A1 | build spec 7.1–7.8 | Affected code, fix, tests, PR |
| 7 | Write API A2 (decision, SSE) + dashboard D3–D5 | this file | Approve from the UI, live activity |
| 8 | Replay mode, E2E, rehearsal; optional Azure deploy (Z1), Trigger B (B1) | build spec 15 | A demo that can't fail on stage |

**If time runs short, the minimum demo** is steps 1–4 plus the upgrade-detail page fed by a recorded
fixture for steps 5–6 (clearly labelled "recorded run"). Never show invented numbers as live.

---

## 3. Backend additions needed by the dashboard

The dashboard reads one API. Build it as `backend/app/api/` (FastAPI) on top of read-only query
functions in `backend/app/api/queries.py` (all SQL there), so the UI never knows about tables.

### 3.1 Risk and status vocabulary (single source of truth, backend `app/core/vocab.py` and frontend `src/lib/vocab.ts`)

| `risk` value | Label | Colour token |
| --- | --- | --- |
| `critical` | Critical | `--critical` |
| `high` | High | `--high` |
| `medium` | Medium | `--medium` |
| `low` | Low | `--low` |
| `none` | Up to date | `--rule` |

| `status` (upgrade) | Label | Pill style |
| --- | --- | --- |
| `not_started` | Not analysed yet | neutral |
| `queued` | Queued | neutral |
| `analyzing` | Analyzing | run |
| `fixing` | Writing a fix | run |
| `validating` | Testing the fix | run |
| `awaiting_approval` | Awaiting approval | wait |
| `approved` | Approved | done |
| `pr_opened` | Pull request opened | done |
| `rejected` | Rejected | neutral |
| `needs_engineer` | Needs an engineer | fail |
| `report_only` | Report only | neutral |
| `up_to_date` | No action | done |
| `failed` | Failed | fail |

| Finding `source` | Label shown |
| --- | --- |
| `pattern` | Code search |
| `runtime_deprecation` | Runtime warning |
| `test_failure` | Failing test |
| `llm` | Reviewed |

### 3.2 Endpoints (read first, then write)

| Milestone | Method | Path | Returns |
| --- | --- | --- | --- |
| A0 | GET | `/api/health` | `{db, migrations, llm_mode, worker}` |
| A0 | GET | `/api/scans` | `ScanSummary[]` newest first |
| A0 | GET | `/api/scans/{scan_id}/dependencies` (`latest` allowed) | `DependencyRow[]` |
| A0 | GET | `/api/dependency-updates/{id}` | `DependencyDetail` |
| A1 | GET | `/api/scans/{scan_id}/overview` | `Overview` |
| A1 | GET | `/api/upgrades?status=&scan_id=` | `UpgradeCard[]` |
| A1 | GET | `/api/upgrades/{run_id}` | `UpgradeDetail` |
| A2 | POST | `/api/scans` `{repo_url}` | `{job_id}` |
| A2 | POST | `/api/upgrades/{run_id}/decision` `{decision, comment}` | `{status}`; 409 if not awaiting approval |
| A2 | GET | `/api/upgrades/{run_id}/events` | SSE stream of `RunEvent` (replay, then live) |
| A2 | POST | `/api/feedback` | `{id}` |

Prefix everything with `/api`; serve the built frontend from `/` in production (or Static Web Apps).
CORS: only `http://localhost:5173` in development.

### 3.3 Response contracts (TypeScript; generate from FastAPI's OpenAPI with `openapi-typescript`)

```ts
export type Risk = "critical" | "high" | "medium" | "low" | "none";
export type UpgradeStatus =
  | "not_started" | "queued" | "analyzing" | "fixing" | "validating" | "awaiting_approval"
  | "approved" | "pr_opened" | "rejected" | "needs_engineer" | "report_only" | "up_to_date" | "failed";
export type FindingSource = "pattern" | "runtime_deprecation" | "test_failure" | "llm";

export interface ScanSummary {
  id: string; repo_url: string | null; repo_name: string; commit_sha: string;
  scanned_at: string;            // ISO-8601 UTC
  dependency_count: number;
}

export interface DependencyRow {
  id: number;                    // dependency_updates.id
  package: string; ecosystem: string; dependency_type: string;
  declared_constraint: string | null;
  current_version: string | null; latest_in_range: string | null; latest_version: string | null;
  update_type: "none" | "patch" | "minor" | "major" | "unknown";
  risk: Risk; status: string;    // dependency_updates.status
  vuln_count: number; max_severity: string | null;
  is_deprecated: boolean; importer_count: number;
  has_upgrade_run: boolean;
}

export interface ChangeItem {
  id: number; kind: string; breaking: boolean; api_symbols: string[];
  description: string; migration_hint: string | null; version: string;
  evidence_url: string; evidence_quote: string;
  affected_lines: number | null; // null until impact analysis has run
}

export interface DependencyDetail extends DependencyRow {
  recommended_target: string | null; requires_manifest_change: boolean;
  runtime_requirement_change: boolean; deprecation_message: string | null;
  summary: string | null; notes_coverage: number | null; source_repo: string | null;
  vulnerabilities: { id: string; severity: string; summary: string | null; fixed_in: string | null; url: string | null }[];
  changes: ChangeItem[];
  importing_files: { path: string; lines: number[] }[];
  upgrade_run_id: string | null;
}

export interface UpgradeCard {
  run_id: string | null; dependency_update_id: number;
  package: string; dependency_type: string;
  from_version: string | null; to_version: string | null;
  update_type: string; risk: Risk; status: UpgradeStatus;
  reason: string;                // one sentence built by the backend from facts (3.5)
  effort_hours: number | null; owner: string | null;
  affected_lines: number; affected_files: number;
  tests_after_fix: { passed: number; total: number } | null;
}

export interface Overview {
  scan: ScanSummary;
  headline: string;              // built by the backend (3.5)
  subline: string;
  risk_counts: Record<Risk, number>;
  board: UpgradeCard[];          // sorted by risk, then effort, then priority
  waiting: UpgradeCard[];        // status = awaiting_approval, oldest first
  security: { advisories: number; items: { package: string; current: string; fixed_in: string | null; severity: string }[] };
  affected_files: { path: string; lines: number; packages: string[]; strongest: FindingSource }[];
}

export interface Finding {
  id: number; file_path: string; line: number; snippet: string; symbol: string | null;
  sources: FindingSource[]; confidence: number; confirmed: boolean | null;
  reason: string; change_id: number | null; github_url: string | null;
}

export interface UpgradeDetail {
  card: UpgradeCard;
  signals: { code_search: number; runtime_warnings: number; failing_tests: number; production_hits: number | null };
  findings: Finding[];           // grouped by file in the UI
  dismissed: Finding[];          // confirmed = false
  changes: ChangeItem[];
  telemetry: { signature: string; route: string | null; count: number; last_seen: string | null; matches: string | null }[];
  plan: { severity: Risk; effort_hours: number; rationale: string; steps: string[]; validation: string[] } | null;
  patch: { attempt: number; method: string; diff: string; manifest_change: string | null } | null;
  tests: { before: TestResult | null; after: TestResult | null };
  approval: { decision: "approved" | "rejected"; decided_by: string; decided_at: string; comment: string | null } | null;
  pull_request: { url: string; number: number | null; kind: "pull_request" | "issue" } | null;
  events: RunEvent[];
}
export interface TestResult { passed: number; total: number; duration_ms: number; output_tail: string }
export interface RunEvent { id: number; node: string; status: "started" | "finished" | "waiting" | "error"; summary: string | null; ts: string }
```

Test counts: parse `passed/total` from the test runner's summary line in `validations.output_tail`
(mocha prints `N passing` and `M failing`) into the `validations.tests_passed` and `tests_total` columns
(already in migration 003 in the build spec).

### 3.4 Overview queries (tested against migrations 002 + 003 with sample rows)

```sql
-- Risk counts for the scan
SELECT coalesce(risk_level, 'none') AS risk, count(*) AS n
FROM dependency_updates WHERE scan_id = :scan GROUP BY 1;

-- The upgrade board
SELECT du.id AS dependency_update_id, du.name AS package, du.dependency_type,
       du.current_version AS from_version,
       coalesce(ur.to_version, du.recommended_target, du.latest_version) AS to_version, du.update_type,
       coalesce(ur.risk_level, du.risk_level, 'none') AS risk, ur.id AS run_id,
       coalesce(ur.status, CASE du.status WHEN 'up_to_date' THEN 'up_to_date' ELSE 'not_started' END) AS status,
       rp.effort_hours, du.vuln_count, du.max_severity, du.importer_count,
       (SELECT count(*) FROM impact_findings f
         WHERE f.upgrade_run_id = ur.id AND coalesce(f.confirmed, 1) = 1) AS affected_lines,
       (SELECT count(DISTINCT f.file_path) FROM impact_findings f
         WHERE f.upgrade_run_id = ur.id AND coalesce(f.confirmed, 1) = 1) AS affected_files
FROM dependency_updates du
LEFT JOIN upgrade_runs ur ON ur.dependency_update_id = du.id
LEFT JOIN remediation_plans rp ON rp.upgrade_run_id = ur.id
WHERE du.scan_id = :scan
  AND (ur.id IS NOT NULL OR du.update_type IN ('patch','minor','major') OR du.vuln_count > 0 OR du.is_deprecated = 1)
ORDER BY CASE coalesce(ur.risk_level, du.risk_level, 'none')
           WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
         coalesce(rp.effort_hours, 999), du.priority_score DESC;

-- Most affected files
SELECT f.file_path AS path, count(*) AS lines, group_concat(DISTINCT ur.package) AS packages,
       max(CASE f.source WHEN 'test_failure' THEN 3 WHEN 'runtime_deprecation' THEN 2
                         WHEN 'pattern' THEN 1 ELSE 0 END) AS strongest
FROM impact_findings f JOIN upgrade_runs ur ON ur.id = f.upgrade_run_id
WHERE ur.scan_id = :scan AND coalesce(f.confirmed, 1) = 1
GROUP BY f.file_path ORDER BY lines DESC, path LIMIT 10;
```

Map `strongest` 3/2/1/0 to `test_failure` / `runtime_deprecation` / `pattern` / `llm`. `upgrade_runs` is
unique per `(scan_id, dependency_update_id)`, so the board never duplicates a package.

### 3.5 Generated sentences (deterministic templates in the backend, never the LLM)

Every sentence is built from counts, so it is always true. `queries.py` returns them; the UI only
renders them.

**Headline** (first rule that applies):

| Condition | Headline |
| --- | --- |
| ≥ 1 awaiting approval and the top board item has production hits | `{n} upgrades need a decision today. {Pkg} {major} would break {lines} lines, and {hits_lines} of them already warn in production.` |
| ≥ 1 awaiting approval | `{n} upgrade(s) need a decision today.` |
| ≥ 1 critical or high, none awaiting | `{n} upgrade(s) carry high risk. Pilot is analysing them.` |
| Updates exist, all low | `Everything that changed is low risk. {n} updates can be applied without code changes.` |
| Nothing to do | `All {deps} dependencies are up to date.` |

Use singular and plural correctly (`1 upgrade needs`, `2 upgrades need`).

**Subline:** `Pilot checked {deps} dependencies against their registries, release notes and your code. Every finding below links to its evidence.`

**Board `reason`** (first rule that applies):

| Condition | Reason |
| --- | --- |
| Findings exist | `{lines} lines in {files} files use APIs removed or changed in {to_major}` + `; {k} already warn in production` if telemetry matched |
| Vulnerabilities | `Security fix for {advisory summary, ≤ 60 chars}` + `; no code changes needed` if no findings |
| Runtime requirement change | `Requires {engines_target}` |
| Deprecated | `Deprecated: {message, ≤ 60 chars}` |
| Analysis not run yet | `{update_type} update; not analysed yet` |
| No findings after analysis | `No breaking change touches this repo` |

### 3.6 Fixture mode (so the UI can be built before the backend is finished)

- `frontend/src/fixtures/*.json`: one file per endpoint, shaped exactly like 3.3. Start from the sample data in
  `dashboard-mockup.html` (the `data` object).
- `VITE_API_MODE=fixtures` makes the API client return fixtures (with a 200 ms delay to exercise loading
  states); `live` calls the backend. A small "Sample data" badge shows in the rail whenever fixtures are on,
  so nobody mistakes them for real results in the demo.

---

## 4. Dashboard: product spec

**Audience:** tech leads deciding which upgrades to act on; engineers approving fixes; judges watching
the demo. **Job:** answer, in this order: what will break, where, how sure we are, what it costs, and
what needs my decision.

### 4.1 Pages and routes

| Route | Page | Shows | Primary action |
| --- | --- | --- | --- |
| `/` → `/overview` | **Overview (critical analysis)** | Headline sentence; the **upgrade board**; waiting-for-you list; risk across all dependencies; security advisories; most affected files | Open an upgrade |
| `/dependencies` | **Dependencies** | Table of every declared package: risk, package, type, current, latest in range, latest, update type, advisories, used in | Filter; open a dependency |
| `/dependencies/:id` | **Dependency detail** | Summary, breaking changes with quotes and links, advisories, deprecation, importing files | Open its upgrade run (if any) |
| `/upgrades` | **Upgrades** | Table of upgrade runs with status and effort | Open one |
| `/upgrades/:runId` | **Upgrade detail** | Header (journey, risk, effort, status, owner) + Approve/Reject; evidence strip; tabs: Affected code, What changed, Production evidence, Plan, Fix and tests, Activity | **Approve and open PR** |
| `/approvals` | **Approvals** | Runs awaiting approval, oldest first | Review |

Global: left rail navigation with the approvals count; top bar with repository selector, scan time and
commit, **Scan now** button.

### 4.2 Overview: the critical analysis view

Built in this order on the page (see the mockup):

1. **Headline** (3.5): one or two sentences, 30 px, max 30 characters wide per line before wrapping. It is
   the answer, not a title.
2. **Upgrade board** (the page's one bold element): one row per upgrade, risk bar on the left edge, package
   + dependency type, the **version journey** `4.21.2 → 5.1.0` in 26 px tabular figures with the update kind,
   the `reason` sentence, risk label + status pill on the right. The whole row is a link.
3. **Right column:** *Waiting for you* (approvals with test results), *Risk across N dependencies* (one
   stacked bar + legend with counts; the bar has an `aria-label` with the same numbers), *Security* (advisory
   count and the fix version).
4. **Most affected files:** file path (mono), line count, packages, strongest evidence.

Sorting: risk (critical first), then effort ascending (quick wins first within a risk level), then priority.

### 4.3 Upgrade detail tabs

| Tab | Content | Rules |
| --- | --- | --- |
| **Affected code** (default) | Findings grouped by file: line number, the code line with the matched API highlighted, one-sentence reason with the fix, and three source chips (Code search, Runtime warning, Failing test) with checkmarks on the ones that found it | Highlight with `<mark>` on the exact matched text; dismissed findings collapsed under "N lines were checked and marked not affected"; line numbers link to GitHub (`#L<line>`) |
| **What changed** | Each change: title, version, breaking flag (red left rule), "affects N lines here" or "nothing here uses it", the verbatim quote, link to the release notes | Never show a change without its quote and link |
| **Production evidence** | Table: log message, route, count, last seen, matched file:line | Empty state: "No matching log lines in the last 7 days." |
| **Plan** | Why this risk (one sentence), numbered steps, numbered validation checks | Numbered because they're sequences |
| **Fix and tests** | Unified diff by file (red/green lines); two result boxes: tests on the target **without** the fix, and **with** the fix (`18 of 23 pass` / `23 of 23 pass`), attempt number and duration | Diff text comes from `patches.diff` (git), rendered read-only |
| **Activity** | Timeline from `run_events`: time, step, one-line summary; the waiting step marked in amber | Live via SSE while the run is active |

**Evidence strip** (above the tabs): four numbers: lines found by code search, runtime deprecation
warnings, tests failing on the target without the fix, matching production log lines (7 days). This
strip is what convinces engineers; keep it on every upgrade.

**Approve:** enabled only when `status = awaiting_approval`. Opens a dialog that says exactly what will
happen ("Pilot opens a pull request on {repo} with the {pkg} upgrade, {n} code changes and the evidence.
Your normal review and CI still apply."), an optional note, and **Approve and open PR**. After success:
toast "Approved. Pull request #{n} opened." and the status pill updates. **Reject** asks for an optional
reason and records it. Disable both buttons while the request is in flight; on 409, refresh and show
"This upgrade was already decided by {name}."

### 4.4 States and copy

| State | What to show |
| --- | --- |
| Loading | Skeleton rows with the real layout (no spinners in the middle of the page) |
| Empty (no scan) | "No scans yet. Scan a repository to see its dependencies." + **Scan now** |
| Empty (board) | "All {n} dependencies are up to date." |
| Error | What failed and what to do: "Couldn't load upgrades. The API at {url} didn't respond. Check that the backend is running, then retry." + **Retry** |
| Fixture mode | "Sample data" badge in the rail |

Copy rules: sentence case everywhere; plain verbs; buttons say what happens ("Approve and open PR",
"Scan now", "Retry"); no all-caps labels; no emoji; numbers always with units (`4.5 h`, `7 days`,
`23 of 23 pass`).

---

## 5. Dashboard: design system (match `dashboard-mockup.html`)

### 5.1 Tokens (CSS variables on `:root`; dark values under `prefers-color-scheme: dark`)

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--ink` | `#1C2632` | `#E6ECF2` | Text, primary button |
| `--ink-2` | `#4A5765` | `#B3BECA` | Secondary text |
| `--ink-3` | `#6B7785` | `#8C98A6` | Muted text, "from" version |
| `--bg` | `#FAFBFC` | `#141B23` | Page |
| `--panel` | `#EEF2F5` | `#1C252F` | Neutral pill, hovers |
| `--panel-2` | `#F4F6F8` | `#18212A` | Rail, file headers |
| `--white` | `#FFFFFF` | `#1A232C` | Surfaces (board, tables) |
| `--rule` | `#D6DDE4` | `#2C3743` | Borders, "up to date" |
| `--critical` / `--critical-bg` | `#C4321C` / `#FBEAE6` | `#F0715C` / `#3A1F1B` | Critical, failures, removed lines |
| `--high` / `--high-bg` | `#B86E00` / `#FDF1DD` | `#E7A440` / `#36291A` | High, awaiting approval |
| `--medium` / `--medium-bg` | `#2F6CB0` / `#E6EFF9` | `#6FA6E8` / `#1C2C40` | Medium, running, links, focus |
| `--low` / `--low-bg` | `#2E7A4E` / `#E5F2EA` | `#67C08C` / `#1A3226` | Low, passing, added lines |

Colour means something every time it appears. No gradients, no decorative colour, no shadows except
the focus ring.

### 5.2 Type

- **Public Sans** (400, 500, 600, 700) for everything, with `font-variant-numeric: tabular-nums`.
- **JetBrains Mono** (400, 500) only for code, file paths, commit SHAs and diffs, never for labels.
- Scale: 30/24 (page headline desktop/mobile), 34 (upgrade detail journey), 26 (board journey), 18 (section
  heading), 15 (body), 14 (tables), 13 (secondary), 12.5 (pills).
- Self-host both fonts in production (`@fontsource/public-sans`, `@fontsource/jetbrains-mono`) so the
  dashboard works without Google Fonts.

### 5.3 Layout

- Rail 220 px + content (max 1280 px, 32 px side padding); below 760 px the rail becomes a top bar and side
  padding is 16 px.
- Overview: `minmax(0, 1fr) 320px` columns; below 1100 px one column (use `minmax(0,1fr)` so long content
  never causes horizontal scroll).
- Radius 4 px (controls) and 8 px (surfaces). Borders 1 px `--rule`.
- Board row grid: `6px minmax(130px,1.1fr) minmax(250px,1.6fr) minmax(0,2fr) 150px`; on narrow screens the
  journey and reason wrap under the package.

### 5.4 Accessibility and quality floor

- Visible focus ring (`2px solid var(--focus)`) on every interactive element; the page container gets
  programmatic focus on route change without a visible outline.
- Tabs follow the WAI-ARIA tabs pattern (Radix Tabs does this); dialog traps focus (Radix Dialog).
- Every colour-coded item also has text (risk label, pill text, chip text).
- Charts have an `aria-label` with the same numbers.
- `prefers-reduced-motion` respected; the only motion is hover background and dialog open.
- No horizontal scroll at 360 px wide (tables scroll inside their own container).
- Lighthouse accessibility ≥ 95 on Overview and Upgrade detail.

---

## 6. Frontend implementation

### 6.1 Stack

Vite + React 18 + TypeScript (strict), React Router, TanStack Query, Tailwind CSS mapped to the tokens
above (or plain CSS modules with the same variables, whichever the frontend owner is faster with),
Radix primitives (via shadcn/ui, restyled to the tokens) for Tabs, Dialog, Tooltip, Toast,
`react-diff-viewer-continued` (or the simple unified renderer in the mockup) for diffs, `openapi-typescript`
for types, Vitest + Testing Library for tests, Playwright for one smoke test.

### 6.2 Structure

```
frontend/
├── index.html
├── src/
│   ├── main.tsx, App.tsx            # router, QueryClientProvider
│   ├── styles/tokens.css            # Section 5.1 exactly
│   ├── api/
│   │   ├── types.ts                 # generated (npm run gen:types)
│   │   ├── client.ts                # fetch wrapper: base URL, JSON, errors → ApiError
│   │   ├── fixtures.ts              # VITE_API_MODE=fixtures
│   │   └── hooks.ts                 # useOverview, useDependencies, useUpgrade, useDecision, useRunEvents
│   ├── lib/vocab.ts                 # Section 3.1 labels and colours
│   ├── components/
│   │   ├── layout/Rail.tsx, TopBar.tsx, Page.tsx
│   │   ├── RiskLabel.tsx, StatusPill.tsx, SourceChips.tsx, Journey.tsx
│   │   ├── UpgradeBoard.tsx, BoardRow.tsx
│   │   ├── RiskStack.tsx, WaitingList.tsx, SecurityNote.tsx, AffectedFiles.tsx
│   │   ├── EvidenceStrip.tsx, FindingsByFile.tsx, ChangeList.tsx, TelemetryTable.tsx,
│   │   │   PlanView.tsx, DiffView.tsx, TestResults.tsx, ActivityTimeline.tsx
│   │   ├── ApproveDialog.tsx, RejectDialog.tsx
│   │   └── states/Skeleton.tsx, EmptyState.tsx, ErrorState.tsx
│   ├── pages/Overview.tsx, Dependencies.tsx, DependencyDetail.tsx, Upgrades.tsx, UpgradeDetail.tsx, Approvals.tsx
│   └── fixtures/*.json
└── tests/ (vitest) + e2e/smoke.spec.ts (playwright)
```

### 6.3 Data and live updates

- TanStack Query keys: `["overview", scanId]`, `["dependencies", scanId]`, `["upgrade", runId]`,
  `["upgrades", status]`. `staleTime` 10 s; refetch on window focus.
- `useRunEvents(runId)`: one `EventSource` on `/api/upgrades/{runId}/events`; append events by `id`
  (dedupe); on a `finished` or `waiting` event, invalidate `["upgrade", runId]` and `["overview"]`; close
  when the run reaches a final status. Reconnect is automatic (`Last-Event-ID`).
- Decision: `useMutation` → POST; on success invalidate the upgrade, overview, upgrades and approvals keys.
- Errors: `ApiError` carries status and message; pages render `ErrorState` with **Retry**.

### 6.4 Backend endpoints module

```
backend/app/api/
├── main.py          # FastAPI app, CORS, routers, startup: migrations, worker thread (demo)
├── queries.py       # all read SQL for the dashboard (Section 3.4) and sentence templates (3.5)
├── routes_scans.py, routes_dependencies.py, routes_upgrades.py, routes_feedback.py
└── sse.py           # replay run_events after Last-Event-ID, then poll every 500 ms for new rows
```

Run: `uvicorn app.api.main:app --reload --port 8000` from `backend/`. Vite proxies `/api` to it
(`server.proxy` in `vite.config.ts`).

---

## 7. Plan for the remaining time (6 people)

| Person | Today (Wed) | Thu | Fri morning |
| --- | --- | --- | --- |
| Ingestion owner | Apply patch, re-scan sandbox; Azure OpenAI deployments + keys to everyone (Z0) | Core C0/C1 with P5 or Z1 deploy | Rehearse |
| Kushal (Step 2) | M7-lite `run --no-llm` writing `dependency_updates`; then M3 OSV | M4, M5, M6, full M7 on the sandbox | Replay cache, fixes |
| P3 (impact) | Scope query + pattern search on the sandbox (I1) | Runtime + upgrade probes, LLM confirm, telemetry (I2) | Eval numbers |
| P4 (sandbox, fix, PR) | **Sandbox repo first** (S1) | Remediation R1, PR A1 | Trigger B if time |
| P5 (core, graph, API) | API A0 over `dependency_updates`; C0 | A1, G1 graph + worker, A2 decision + SSE | E2E in replay |
| P6 (dashboard) | D0 + D1 on fixtures (from the mockup data) | D2–D4 on the live API | D5 polish, deck |

### 7.1 Dashboard milestones

| ID | Milestone | Acceptance |
| --- | --- | --- |
| D0 | Scaffold: Vite + TS + router + Query + tokens + Rail/TopBar + fixture mode + types | Matches mockup shell in light and dark; `npm run build` and `npm test` pass |
| D1 | Overview: headline, board, right column, affected files, all states | Pixel-close to the mockup at 1440 px and 390 px; board rows are links; no horizontal scroll at 360 px |
| D2 | Dependencies table with filters + Dependency detail | Filters (All, Needs action, Major, Security, Dev) work on fixtures and live data |
| D3 | Upgrade detail: header, evidence strip, six tabs | Each tab renders its fixture and its empty state |
| D4 | Approvals page, Approve/Reject dialogs, SSE activity | Approve on the live API moves the run to `pr_opened` and the Overview updates without reload |
| D5 | Polish: accessibility pass, Lighthouse ≥ 95, Playwright smoke test of the golden path | Smoke test green in CI |

### 7.2 API milestones

| ID | Milestone | Acceptance |
| --- | --- | --- |
| A0 | FastAPI app, health, scans, dependencies, dependency detail | Responses validate against the TS types (a pytest compares OpenAPI schemas with fixtures) |
| A1 | Overview, upgrades list, upgrade detail; sentence templates | Template unit tests for every row of 3.5 (singular, plural, empty) |
| A2 | Decision endpoint, SSE, scan trigger | Decision resumes the graph; SSE replays after a reconnect |

**Cut order for the dashboard if time runs out:** Dependency detail page (link to the upgrade instead) →
dark mode → Reject reason dialog (keep the button) → Production evidence tab (show the evidence strip only).
Never cut: Overview board, Affected code tab, What changed with quotes, Approve.

---

## 8. Prompts for Claude Code

> **Dashboard D0 + D1:** Read `docs/specs/codebase-pilot-status-and-dashboard-spec.md` Sections 3–6 and open
> `docs/design/dashboard-mockup.html`. Create `frontend/` as specified in 6.1–6.2 with fixture mode on.
> Build D0 and D1 only. Convert the mockup's sample data into `src/fixtures/*.json` shaped like the types in
> 3.3. Match the mockup's tokens, type and layout. Run `npm run build` and `npm test`, take screenshots at
> 1440 px and 390 px, and stop.

> **API A0:** Read Section 3 of `docs/specs/codebase-pilot-status-and-dashboard-spec.md` and
> `backend/app/updates/store.py`. Create `backend/app/api/` with `main.py`, `queries.py` and the A0 routes.
> All SQL in `queries.py`. Add tests with a temporary SQLite DB built from the existing fixtures in
> `backend/tests/updates/conftest.py`. Run `pytest -q` and stop.

> **Step 2 M7-lite:** Read the Step 2 spec Sections 5.5, 5.10 and 5.11. Add `cli run --no-llm` that, for the
> latest scan, resolves versions for every npm dependency and upserts `dependency_updates` with priority,
> risk (rules only) and status. One package failing must not stop the run. Add tests and stop.
