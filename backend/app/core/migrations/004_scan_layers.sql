-- 004: scan layers (S1-S3), scan requests with stages (S2), channels and schedules (S6), change events (S7).
-- Everything is IF NOT EXISTS: the runner replays migrations and a partly migrated database is repaired.

-- One row per lockfile entry (so nested copies of a package are separate rows).
CREATE TABLE IF NOT EXISTS sbom_components (
    scan_id     TEXT NOT NULL,
    location    TEXT NOT NULL,                 -- lockfile key: node_modules/a/node_modules/b
    purl        TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    scope       TEXT NOT NULL,                 -- 'runtime' | 'dev' | 'optional' | 'peer'
    direct      INTEGER NOT NULL DEFAULT 0,
    path        JSON NOT NULL,                 -- shortest chain from the root manifest: ["express","body-parser","qs"]
    PRIMARY KEY (scan_id, location)
);
CREATE INDEX IF NOT EXISTS idx_sbom_components_name ON sbom_components (scan_id, name);

-- Advisories that hit a package of the scan, with the verdict code computed (Part 3.3).
CREATE TABLE IF NOT EXISTS scan_vulns (
    scan_id     TEXT NOT NULL,
    vuln_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    purl        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    direct      INTEGER NOT NULL DEFAULT 0,
    path        JSON NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'unknown',
    summary     TEXT,
    aliases     JSON,
    fixed_in    TEXT,
    verdict     TEXT NOT NULL,                 -- 'applies' | 'does_not_apply' | 'not_shipped' | 'undetermined'
    reason      TEXT NOT NULL,
    sources     JSON NOT NULL,                 -- ["osv"] | ["osv","dependabot"]
    url         TEXT,
    PRIMARY KEY (scan_id, vuln_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_scan_vulns_scan ON scan_vulns (scan_id, verdict);

-- A request to scan a repository, and the stages it goes through (what the progress page streams).
CREATE TABLE IF NOT EXISTS scan_requests (
    id          TEXT PRIMARY KEY,
    repo_url    TEXT NOT NULL,
    branch      TEXT,
    scope       JSON NOT NULL,
    only        JSON,
    status      TEXT NOT NULL DEFAULT 'queued',   -- 'queued' | 'running' | 'complete' | 'failed' | 'cancelled'
    scan_id     TEXT,
    job_id      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS scan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,                 -- clone | layer1 | sbom | internal | layer2 | vulnerabilities | versions | changes | impact
    status      TEXT NOT NULL,                 -- 'running' | 'done' | 'skipped' | 'error'
    summary     TEXT,
    data        JSON,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scan_events_request ON scan_events (request_id, id);

-- Where Pilot reads changes from and reports to. `config` holds non-secret values only; `secret_ref` is a NAME.
CREATE TABLE IF NOT EXISTS channels (
    repo            TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- slack | email | confluence | github_webhook | dependabot | bot_prs | dashboard_issue
    enabled         INTEGER NOT NULL DEFAULT 0,
    config          JSON NOT NULL DEFAULT '{}',
    secret_ref      TEXT,
    status          TEXT NOT NULL DEFAULT 'not_connected',
    last_checked_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (repo, kind)
);

CREATE TABLE IF NOT EXISTS schedules (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,            -- rescan | dependabot_alerts | releases | internal_sources
    repo             TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run_at      TEXT,
    next_run_at      TEXT,
    UNIQUE (source, repo)
);

-- One row per detected change, whatever its source (Part 3.1).
CREATE TABLE IF NOT EXISTS change_events (
    event_id      TEXT PRIMARY KEY,
    origin        TEXT NOT NULL,               -- 'external' | 'internal'
    source        TEXT NOT NULL,
    repo          TEXT NOT NULL,
    scan_id       TEXT,
    subject_kind  TEXT NOT NULL,
    subject_name  TEXT NOT NULL,
    from_version  TEXT,
    to_version    TEXT,
    detected_at   TEXT NOT NULL,
    payload       JSON NOT NULL,
    status        TEXT NOT NULL DEFAULT 'detected',
    run_id        TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_events_repo ON change_events (repo, detected_at);

CREATE TABLE IF NOT EXISTS change_cursors (
    source      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    cursor      TEXT,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (source, repo)
);
