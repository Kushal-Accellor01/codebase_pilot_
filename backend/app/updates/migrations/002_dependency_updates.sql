PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS registry_packages (
    ecosystem           TEXT NOT NULL,
    name                TEXT NOT NULL,
    latest_version      TEXT,
    repository_url      TEXT,
    homepage_url        TEXT,
    is_deprecated       INTEGER NOT NULL DEFAULT 0,
    deprecation_message TEXT,
    registry_modified   TEXT,
    etag                TEXT,
    status              TEXT NOT NULL DEFAULT 'ok',      -- 'ok' | 'not_found' | 'error'
    error               TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (ecosystem, name)
);

CREATE TABLE IF NOT EXISTS registry_versions (
    ecosystem             TEXT NOT NULL,
    name                  TEXT NOT NULL,
    version               TEXT NOT NULL,
    published_at          TEXT,
    is_prerelease         INTEGER NOT NULL DEFAULT 0,
    deprecation_message   TEXT,
    engines_json          JSON,
    peer_dependencies_json JSON,
    PRIMARY KEY (ecosystem, name, version),
    FOREIGN KEY (ecosystem, name) REFERENCES registry_packages(ecosystem, name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS package_sources (
    ecosystem       TEXT NOT NULL,
    name            TEXT NOT NULL,
    host            TEXT,                                 -- 'github'
    owner           TEXT,
    repo            TEXT,
    directory       TEXT,
    default_branch  TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    tag_format      TEXT,
    changelog_path  TEXT,
    method          TEXT NOT NULL,                        -- 'override' | 'registry' | 'deps_dev' | 'homepage' | 'manual'
    confidence      TEXT NOT NULL,                        -- 'high' | 'medium' | 'low'
    verified        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'resolved',     -- 'resolved' | 'unknown'
    resolved_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (ecosystem, name)
);

CREATE TABLE IF NOT EXISTS vulnerabilities (
    id              TEXT PRIMARY KEY,                     -- OSV id
    aliases         JSON,
    summary         TEXT,
    severity        TEXT,                                 -- 'critical' | 'high' | 'medium' | 'low' | 'unknown'
    cvss_vector     TEXT,
    published_at    TEXT,
    modified_at     TEXT,
    url             TEXT,
    raw             JSON NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS update_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ecosystem       TEXT NOT NULL,
    name            TEXT NOT NULL,
    from_version    TEXT NOT NULL,
    to_version      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_sha256    TEXT NOT NULL,
    summary         TEXT,
    has_breaking    INTEGER NOT NULL DEFAULT 0,
    notes_coverage  REAL,
    analysis_json   JSON NOT NULL,
    dropped_json    JSON,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (ecosystem, name, from_version, to_version, prompt_version, input_sha256)
);

CREATE TABLE IF NOT EXISTS update_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id      INTEGER NOT NULL REFERENCES update_analyses(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    breaking         INTEGER NOT NULL,
    api_symbols      JSON NOT NULL,
    description      TEXT NOT NULL,
    migration_hint   TEXT,
    version          TEXT NOT NULL,
    evidence_url     TEXT NOT NULL,
    evidence_quote   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_updates (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id                    TEXT NOT NULL REFERENCES scans(id),
    dep_node_id                TEXT NOT NULL,
    ecosystem                  TEXT NOT NULL,
    name                       TEXT NOT NULL,
    manifest_file_id           TEXT NOT NULL DEFAULT '',  -- NOT NULL so the UNIQUE upsert works
    dependency_type            TEXT NOT NULL DEFAULT 'unknown',
    declared_constraint        TEXT,
    current_version            TEXT,
    current_version_basis      TEXT,
    latest_in_range            TEXT,
    latest_version             TEXT,
    recommended_target         TEXT,
    update_type                TEXT,
    requires_manifest_change   INTEGER NOT NULL DEFAULT 0,
    versions_behind            INTEGER,
    runtime_requirement_change INTEGER NOT NULL DEFAULT 0,
    is_deprecated              INTEGER NOT NULL DEFAULT 0,
    deprecation_message        TEXT,
    vuln_count                 INTEGER NOT NULL DEFAULT 0,
    max_severity               TEXT,
    min_safe_version           TEXT,
    importer_count             INTEGER NOT NULL DEFAULT 0,
    priority_score             REAL,
    risk_level                 TEXT,                      -- 'critical' | 'high' | 'medium' | 'low' | 'none'
    analysis_id                INTEGER REFERENCES update_analyses(id),
    status                     TEXT NOT NULL DEFAULT 'pending',
        -- 'pending' | 'up_to_date' | 'analyzed' | 'notes_unavailable' | 'source_unknown' | 'not_in_registry'
        -- | 'unsupported_ecosystem' | 'invalid_declaration' | 'error'
    error                      TEXT,
    checked_at                 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (scan_id, dep_node_id, manifest_file_id)
);

CREATE TABLE IF NOT EXISTS dependency_update_vulns (
    update_id   INTEGER NOT NULL REFERENCES dependency_updates(id) ON DELETE CASCADE,
    vuln_id     TEXT    NOT NULL REFERENCES vulnerabilities(id),
    fixed_in    TEXT,
    PRIMARY KEY (update_id, vuln_id)
);

CREATE TABLE IF NOT EXISTS release_notes (
    ecosystem       TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    source          TEXT NOT NULL,                        -- 'github_release' | 'changelog_file' | 'compare_commits'
    url             TEXT NOT NULL,
    published_at    TEXT,
    body_md         TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (ecosystem, name, version, source)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           TEXT PRIMARY KEY,
    scan_id      TEXT NOT NULL REFERENCES scans(id),
    step         TEXT NOT NULL,                           -- 'dependency_updates'
    status       TEXT NOT NULL,                           -- 'running' | 'succeeded' | 'partial' | 'failed'
    stats        JSON,
    error        TEXT,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_updates_scan    ON dependency_updates(scan_id, priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_changes_anal    ON update_changes(analysis_id);
CREATE INDEX IF NOT EXISTS idx_edges_scan_type ON edges(scan_id, type);
CREATE INDEX IF NOT EXISTS idx_nodes_scan_type ON nodes(scan_id, type);
