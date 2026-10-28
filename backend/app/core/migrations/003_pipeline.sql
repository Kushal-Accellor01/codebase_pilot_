PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                 -- 'scan' | 'dependency_updates' | 'upgrade_run' | 'resume_upgrade'
    payload      JSON NOT NULL,
    dedupe_key   TEXT UNIQUE,                   -- e.g. 'upgrade_run:<scan>:npm:express:4.21.2->5.1.0'
    status       TEXT NOT NULL DEFAULT 'queued',-- 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_after    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    locked_by    TEXT,
    locked_at    TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, run_after);

CREATE TABLE IF NOT EXISTS upgrade_runs (
    id                   TEXT PRIMARY KEY,      -- also the LangGraph thread_id
    scan_id              TEXT NOT NULL REFERENCES scans(id),
    dependency_update_id INTEGER NOT NULL REFERENCES dependency_updates(id),
    ecosystem            TEXT NOT NULL,
    package              TEXT NOT NULL,
    from_version         TEXT,
    to_version           TEXT,
    status               TEXT NOT NULL DEFAULT 'queued',
        -- 'queued' | 'analyzing' | 'fixing' | 'validating' | 'awaiting_approval' | 'approved'
        -- | 'rejected' | 'pr_opened' | 'needs_engineer' | 'report_only' | 'failed'
    current_node         TEXT,
    risk_level           TEXT,
    error                TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (scan_id, dependency_update_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    node        TEXT NOT NULL,
    status      TEXT NOT NULL,                  -- 'started' | 'finished' | 'waiting' | 'error'
    summary     TEXT,
    data        JSON,
    duration_ms INTEGER,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);

CREATE TABLE IF NOT EXISTS impact_findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upgrade_run_id  TEXT NOT NULL REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    change_id       INTEGER REFERENCES update_changes(id),
    file_path       TEXT NOT NULL,
    line            INTEGER NOT NULL,
    end_line        INTEGER,
    snippet         TEXT NOT NULL,
    symbol          TEXT,
    source          TEXT NOT NULL,              -- 'pattern' | 'runtime_deprecation' | 'test_failure' | 'llm'
    confidence      REAL NOT NULL,
    confirmed       INTEGER,                    -- LLM verdict: 1 | 0 | NULL
    reason          TEXT,
    UNIQUE (upgrade_run_id, file_path, line, symbol)
);

CREATE TABLE IF NOT EXISTS telemetry_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upgrade_run_id  TEXT NOT NULL REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,              -- 'seeded_logs' | 'datadog' | ...
    signature       TEXT NOT NULL,
    count           INTEGER NOT NULL,
    first_seen      TEXT,
    last_seen       TEXT,
    sample          TEXT,
    related_symbol  TEXT
);

CREATE TABLE IF NOT EXISTS remediation_plans (
    upgrade_run_id     TEXT PRIMARY KEY REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    severity           TEXT NOT NULL,
    effort_hours       REAL NOT NULL,
    rationale          TEXT NOT NULL,
    steps_json         JSON NOT NULL,
    validation_json    JSON NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS patches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upgrade_run_id  TEXT NOT NULL REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    method          TEXT NOT NULL,              -- 'codemod' | 'llm_edits' | 'codemod+llm'
    edits_json      JSON NOT NULL,
    diff            TEXT NOT NULL,              -- produced by git, never by the LLM
    manifest_change TEXT,
    rejected_edits  JSON,
    UNIQUE (upgrade_run_id, attempt)
);

CREATE TABLE IF NOT EXISTS validations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id        INTEGER NOT NULL REFERENCES patches(id) ON DELETE CASCADE,
    command         TEXT NOT NULL,
    exit_code       INTEGER,
    passed          INTEGER NOT NULL,
    tests_passed    INTEGER,                    -- parsed from the test runner output when possible
    tests_total     INTEGER,
    duration_ms     INTEGER,
    output_tail     TEXT,
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS approvals (
    upgrade_run_id  TEXT PRIMARY KEY REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    decision        TEXT NOT NULL,              -- 'approved' | 'rejected'
    decided_by      TEXT NOT NULL,
    comment         TEXT,
    decided_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS pull_requests (
    upgrade_run_id  TEXT PRIMARY KEY REFERENCES upgrade_runs(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,              -- 'pull_request' | 'issue'
    repo            TEXT NOT NULL,
    number          INTEGER,
    url             TEXT,
    branch          TEXT,
    state           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    JSON NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,                  -- 'impact_finding' | 'update_change'
    target_id   INTEGER NOT NULL,
    verdict     TEXT NOT NULL,                  -- 'correct' | 'not_relevant'
    user_name   TEXT,
    comment     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
