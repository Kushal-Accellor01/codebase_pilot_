"""Connections and the migration runner shared by every step.

Ingestion owns `scans`, `nodes`, `edges` (created by app.database.GraphDatabase). Migrations here add
everything else, in order, and record each in `schema_migrations`.
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path

CORE_DIR = Path(__file__).parent
UPDATES_DIR = CORE_DIR.parent / "updates"

INGESTION_TABLES = ("scans", "nodes", "edges")
MIN_SQLITE = (3, 35)  # the job claim uses UPDATE ... RETURNING


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path


MIGRATIONS = (
    Migration("002_dependency_updates", UPDATES_DIR / "migrations" / "002_dependency_updates.sql"),
    Migration("003_pipeline", CORE_DIR / "migrations" / "003_pipeline.sql"),
)


def connect(db_path: str, *, must_exist: bool = True) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise DatabaseError(f"SQLite {sqlite3.sqlite_version} is too old; 3.35 or newer is required")
    if must_exist and not Path(db_path).is_file():
        raise DatabaseError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    if "schema_migrations" not in table_names(conn):
        return []
    return [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration in order and record it. Returns the versions that were newly recorded.

    All DDL is IF NOT EXISTS, so running again (or on a DB that already has the tables from before
    the runner existed) is a no-op apart from the bookkeeping row.
    """
    missing = [t for t in INGESTION_TABLES if t not in table_names(conn)]
    if missing:
        raise DatabaseError(
            f"Ingestion tables missing: {', '.join(missing)}. Run a scan first (python -m app.main <url>)."
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
    )
    conn.commit()
    done = set(applied_versions(conn))
    newly: list[str] = []
    for migration in MIGRATIONS:
        # Re-running a recorded migration is harmless and repairs a partially migrated database.
        conn.executescript(migration.path.read_text(encoding="utf-8"))
        if migration.version not in done:
            with conn:
                conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (migration.version,))
            newly.append(migration.version)
    return newly
