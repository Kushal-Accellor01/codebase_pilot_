# Running Codebase Pilot live

What to set up, where to give a repository, how to check it, and what has and has not been proven.

## 1. What you have to provide

| What | Setting (`backend/.env`) | Needed for | How to get it |
| --- | --- | --- | --- |
| Azure OpenAI endpoint, two deployment names, API version | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_STRONG`, `AZURE_OPENAI_DEPLOYMENT_FAST`, `AZURE_OPENAI_API_VERSION` | Reading release notes, confirming findings, writing the plan and the fix | Azure portal / Foundry: deployment **names**, not model names |
| Model credential | `AZURE_OPENAI_API_KEY` (or leave empty and run `az login`) | Same | Key from the resource, or Entra ID |
| GitHub token | `GITHUB_TOKEN` | Release notes beyond 60 requests per hour, opening pull requests and issues | Fine-grained token on the target repository: Contents read and write, Pull requests read and write, Issues read and write, Metadata read. Or a classic token with `repo` |
| Repositories Pilot may run | `EXEC_ALLOWED_REPOS` (comma separated `owner/repo`, or local paths) | Scanning from the dashboard, running `npm install` and `npm test`, fixes and pull requests | You decide. Pilot installs and runs the code of these repositories only |
| Private npm registry (optional) | `NPM_REGISTRY_URL`, `NPM_REGISTRY_TOKEN` | Artifactory / Azure Artifacts | Your registry admin |
| Production logs (optional) | `AZURE_LOG_ANALYTICS_WORKSPACE_ID`, plus the table and column settings and `service:` in the repo's `.codebase-pilot.yml` | "Already warns in production" evidence | Log Analytics workspace id; the identity needs the **Log Analytics Reader** role. Without it Pilot reads `logs/*.jsonl` or `SEEDED_LOGS_DIR` |
| Dashboard protection (optional) | `PILOT_API_TOKEN` | Stops anyone who can reach the API from approving | Make up a long random string; give the same to the dashboard (below) |
| Push webhook (optional) | `GITHUB_WEBHOOK_SECRET` | Re-scan on every push to the default branch | Make up a secret; use the same in the GitHub webhook |
| Scheduled re-scan (optional) | `PILOT_RESCAN_MINUTES` (needs `PILOT_EMBEDDED_WORKER=1`) | Closing finished upgrades without a click | A number, for example `30` |

Everything else has a default. `backend/.env.example` lists all of it.

## 2. Check the machine before the demo

```powershell
cd backend
python -m pip install -r requirements-dev.txt
python -m app.core.preflight --repo https://github.com/OWNER/REPO
```

(or `python scripts/dev.py live-check --repo https://github.com/OWNER/REPO` from the repository root; add
`--no-model` to skip the two model calls.)

It checks, and says how to fix each failure:

- `git`, `node` (18 or newer) and `npm` are installed; the database, workspace and reports folders are writable;
- the Azure settings are complete and **both deployments answer** a tiny request;
- the GitHub token is valid, and for each `owner/repo` in `EXEC_ALLOWED_REPOS`: it exists, is not archived and the token
  **can push** (a read-only token only produces local branches);
- the npm registry answers, OSV answers, Azure Monitor answers (when configured);
- with `--repo`: `git ls-remote` can read that repository without a prompt, and it is in `EXEC_ALLOWED_REPOS`.

Do not start the demo until it prints `Ready.`

## 3. Where to give a repository

All of these end in the same `scan` job, which then runs the whole pipeline.

1. **Dashboard** (http://localhost:5173): the box in the top bar. Paste `https://github.com/owner/repo` and press
   **Scan this repository**. Empty box plus **Scan now** scans the last repository again.
   The repository must be in `EXEC_ALLOWED_REPOS`, otherwise the API answers 403 and the dashboard shows why.
2. **Command line**: `python -m app.orchestrator.cli pipeline https://github.com/owner/repo --only express`
   (`--only` limits it to some packages; leave it out for all). Add `--verbose` to see the calls.
3. **API**: `POST /api/scans` with `{"repo_url": "https://github.com/owner/repo", "only": null}`.
4. **Push webhook**: in the GitHub repository, Settings, Webhooks: payload URL `https://YOUR-HOST/api/webhooks/github`,
   content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, event **Push**. Pushes to the default branch queue a scan.
5. **Schedule**: `PILOT_RESCAN_MINUTES=30` with `PILOT_EMBEDDED_WORKER=1`, or run
   `python -m app.orchestrator.cli rescan --run` from cron / Task Scheduler.

Private repositories: `git clone` runs as you, so `git ls-remote <url>` must work without a prompt (Git Credential
Manager, or an SSH URL). Pilot never passes your token to git or to the repository's code.

Repository requirements: a `package.json` (npm). The Python files are read for imports, but upgrades are npm only.
Pilot runs `npm test` (override with `fixes.test_command` in `.codebase-pilot.yml`), so a repository without a working
test command can be analysed but its fix cannot be proven.

Per repository, optionally commit `.codebase-pilot.yml`:

```yaml
mode: assisted            # shadow (report only) | advisory (issues only) | assisted (PRs after approval)
owners: "@acme/platform"  # reviewer for files without a CODEOWNERS rule
service: shop-api         # container app name for Azure Monitor
notify:
  min_severity: medium
fixes:
  allowed_paths: ["routes/**", "lib/**"]   # Pilot may edit only these
  test_command: "npm test"
```

## 4. Run it

```powershell
cd backend
python -m app.core.preflight --repo https://github.com/OWNER/REPO     # must say Ready
$env:PILOT_EMBEDDED_WORKER = "1"
python -m uvicorn app.api.main:app --port 8000                       # API + worker
# second terminal
cd frontend; npm install; npm run dev                                 # http://localhost:5173
```

With `PILOT_API_TOKEN` set, give the dashboard the token once: open the browser console and run
`localStorage.pilotToken = "<token>"; localStorage.pilotUser = "you@company.com"`. (If you forget, the dashboard asks
the first time you press a button.) The approver recorded on the pull request is then `pilotUser`, not typed text.

## 5. The pull request: what happens and what to look at

1. Scan, updates, impact, plan, fix, tests run. The run stops at **Awaiting approval** (dashboard, Approvals).
2. Read the evidence, then **Approve and open PR** and type your name.
3. With a token and an allowlisted GitHub repository, Pilot creates the branch `pilot/<package>-<version>` from the
   default branch, commits the edited files, `package.json` and the lockfile through the GitHub API, opens the pull
   request (or updates the open one: it is found by branch, never duplicated), and adds the labels `codebase-pilot` and
   `dependencies`. The body has the changes with quotes, the affected lines with links, the three test runs, production
   evidence, who approved, and a hidden run marker.
4. Without a token, or when the repository is local: the branch is pushed into the scanned repository's own clone and the
   body is written to `backend/reports/pr_<run>.md`. Nothing leaves the machine.
5. If the tests still fail after the attempts, or the policy says issue, Approve opens an **issue** with the evidence
   instead (status `Needs an engineer` when the fix failed, `Issue opened` otherwise).
6. After the PR is merged, the next scan (button, webhook or schedule) marks the run **Resolved**. Pilot only calls the PR
   `merged` when GitHub (or the local branch history) confirms it.

Nothing merges automatically. Rejecting a run opens nothing.

## 6. What has been proven, and what has not

Proven by tests that run offline (567 backend tests, 21 frontend tests, a real-browser smoke test): the whole loop with
a scripted model and mocked GitHub; re-scan safety; approvals, rejection and conflicts; local pull requests;
loop closure; the webhook signature; the API token; every GitHub write call (as recorded requests).

Proven live on a developer machine without any credentials (real npm, real GitHub reads, real OSV, real `npm install`
and `npm test` of the sandbox): version resolution, advisories, release-note fetch, the runtime-warning and
failing-test probes (they alone found all 7 planted lines), the worktree and the test parser.

**Only you can prove, with real credentials:** the model answers and the extracted changes are good (Step 2 notes, the
confirmation, the plan, the fix edits); a real pull request is created with the right labels and files; Azure Monitor
returns rows; a private registry works; the webhook reaches your server. `python -m app.core.preflight` tells you which
of these work on your machine; the rest is one real scan of the sandbox repository.

Recording the model's answers once (`LLM_MODE=record`, then `LLM_MODE=replay`) makes the demo repeatable if Wi-Fi or the
model is flaky on the day.

## 7. When something is wrong

| You see | Likely cause |
| --- | --- |
| Dashboard toast: `this repository is not in EXEC_ALLOWED_REPOS` | Add `owner/repo` to the setting and restart the API |
| Run ends `Report only`, "may not be executed" | Same: the repository is not allowlisted |
| Run ends `Needs an engineer`, "no model is configured" | Azure settings missing; run the preflight |
| Pull request is only a local branch | No `GITHUB_TOKEN`, or the repository is not a GitHub slug in `EXEC_ALLOWED_REPOS` |
| `GitHubWriteError: ... 403` | Token lacks Contents / Pull requests write on that repository |
| Scan fails at `git clone` | Private repository without a credential helper; run `git ls-remote <url>` yourself |
| Notes empty, `GitHub calls are limited to 60 per hour` | No token set |
| Dashboard buttons answer 401 | `PILOT_API_TOKEN` is set; give the dashboard the token |

## 8. Scan page, vulnerabilities, channels and the update feed

- **Scan a repository** (`/scan`): paste a link, optionally a branch and package filter, choose what to look for
  (the production-evidence tile starts off), press **Check access first** (runs `git ls-remote` on the server) and then
  **Start scan**. The progress page shows nine steps; steps that are switched off or not built say why. The results
  page has the four numbers, the advisories with a verdict each, the dependency lists and a CycloneDX download.
- **Verdicts** are computed in code: *applies* (a package your code imports leads to it), *does not apply*,
  *not shipped* (development tooling only) and *undetermined*. Without a symbol index, *applies* means the vulnerable
  package is on a path your code loads. Advisories come from OSV for every package in the lockfile, plus Dependabot alerts
  when `GITHUB_TOKEN` has "Dependabot alerts: read".
- **Source mapping** (`/setup/sources`): confirm what Pilot found, or tell it where an unknown package publishes its
  changes. Repositories are used today; changelog URL, Confluence page and Slack channel are saved for a later reader.
- **Channels and schedules** (`/setup/channels`): per repository. Slack and email are the only automatic messages, sent
  when an upgrade needs a decision and when a pull request is opened, with names and counts and no code. Confluence
  publishes one report page when you press Publish. The webhook can be installed from here when `PILOT_PUBLIC_URL` and
  `GITHUB_WEBHOOK_SECRET` are set. Schedules (re-scan, Dependabot alerts, new releases) run inside the API process with
  `PILOT_EMBEDDED_WORKER=1`.
- **Updates** (`/updates`): one card per change, whatever its source, with the quote, the impact and a six-step tracker.

Server secrets for the connectors are named in `.env.example` (`SLACK_BOT_TOKEN`, `SMTP_*`, `CONFLUENCE_*`). The browser
only ever sees whether a named secret is set.

Not in this version: internal contracts (OpenAPI, AsyncAPI, protobuf, topics), the symbol index, reading Confluence and
Slack as change sources, Bitbucket, other model providers.
