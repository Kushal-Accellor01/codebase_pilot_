# Codebase Pilot

Watches a codebase's third-party dependencies. When a new version matters, it shows **which lines of your code
are affected and why, with evidence**, proposes and tests the fix, and opens a pull request after a person approves.

```
scan -> dependency updates -> impact -> plan -> fix + test -> approval -> pull request -> re-scan closes it
```

## Run it offline (no keys, no network): 2 minutes

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/demo_server.py                            # API + worker + scripted model on :8000, seeded
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev                                              # http://localhost:5173
```

Open the dashboard: Overview -> the express upgrade -> read the evidence -> **Approve**. A local pull request
appears (a `pilot/express-5.3.0` branch in the sandbox mirror and a Markdown body under `backend/reports/`).

## Run it for real

1. `backend/.env` from `backend/.env.example`: `GITHUB_TOKEN`, `AZURE_OPENAI_*`. Check the model: `python -m app.core.llm --smoke`.
2. `python scripts/dev.py seed-demo` scans the sandbox and stops at the approval pause.
3. `python scripts/dev.py api` and `python scripts/dev.py web`; `PILOT_EMBEDDED_WORKER=1` runs the worker inside the API.
4. For a GitHub PR instead of a local one, set `GITHUB_TOKEN` and `EXEC_ALLOWED_REPOS=<org>/pilot-sandbox-shop`.

## Layout

| Path | What |
| --- | --- |
| `backend/app/` | ingestion (existing) + `updates/` `impact/` `telemetry/` `planning/` `remediation/` `orchestrator/` `actions/` `api/` `core/` |
| `frontend/` | React + TypeScript dashboard |
| `pilot-sandbox-shop/` | Express 4.21.2 demo target with planted Express 5 breakages |
| `evals/` | answer key and `run_eval.py` (recall 1.0, precision 0.9) |
| `scripts/dev.py` | cross-platform tasks |

## Checks

```bash
cd backend  && ruff check . && pytest -q               # ~520 tests, no network
cd frontend && npm run build && npm test               # type check, build, 19 component tests
cd frontend && npx playwright test                     # real browser, whole stack offline
python evals/run_eval.py --db <db>                     # precision / recall of findings
```

## Rules the code keeps

Deterministic code finds and computes; the model only reads text it is given, and code verifies every claim
(quotes in the notes, edits that apply exactly once, files that exist). Repository code runs only in allowlisted
repos with `--ignore-scripts`, a stripped environment and timeouts. Nothing is merged automatically; a person approves.
