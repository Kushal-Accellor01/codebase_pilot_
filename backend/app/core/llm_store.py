"""SQL for the LLM response cache."""
import json
import sqlite3


def cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row["response"]) if row else None


def cache_put(conn: sqlite3.Connection, key: str, model: str, response: dict) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, model, response) VALUES (?, ?, ?)",
            (key, model, json.dumps(response)),
        )
