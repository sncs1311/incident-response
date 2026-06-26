"""
agent/core/database.py

SQLite incident store. Every incident the system detects, diagnoses,
and acts on is recorded here. This is your audit trail and the data
source for pattern analysis ("which service crashes most often?").
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "/app/data/incidents.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # Rows behave like dicts
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            service     TEXT NOT NULL,
            severity    TEXT NOT NULL,           -- critical / warning / info
            title       TEXT NOT NULL,
            raw_logs    TEXT,                    -- JSON array of relevant log lines
            diagnosis   TEXT,                    -- LLM plain-English diagnosis (Phase 2)
            status      TEXT DEFAULT 'open',     -- open / remediated / escalated / closed
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL REFERENCES incidents(id),
            created_at  TEXT NOT NULL,
            action_type TEXT NOT NULL,           -- restart_service / clear_disk / notify / etc.
            payload     TEXT,                    -- JSON: what exactly was done
            outcome     TEXT,                    -- success / failed / skipped
            error       TEXT                     -- error message if outcome=failed
        );

        CREATE INDEX IF NOT EXISTS idx_incidents_service    ON incidents(service);
        CREATE INDEX IF NOT EXISTS idx_incidents_status     ON incidents(status);
        CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON incidents(created_at);
    """)
    conn.commit()
    conn.close()
    print(f"[db] Initialised at {DB_PATH}")


def create_incident(service: str, severity: str, title: str, raw_logs: str = None) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO incidents (created_at, service, severity, title, raw_logs)
           VALUES (?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), service, severity, title, raw_logs)
    )
    conn.commit()
    incident_id = cur.lastrowid
    conn.close()
    return incident_id


def update_incident(incident_id: int, **fields):
    """Update any fields on an incident by keyword argument."""
    conn = get_connection()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE incidents SET {set_clause} WHERE id = ?",
        (*fields.values(), incident_id)
    )
    conn.commit()
    conn.close()


def log_action(incident_id: int, action_type: str, payload: str, outcome: str, error: str = None):
    conn = get_connection()
    conn.execute(
        """INSERT INTO actions (incident_id, created_at, action_type, payload, outcome, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (incident_id, datetime.utcnow().isoformat(), action_type, payload, outcome, error)
    )
    conn.commit()
    conn.close()


def get_recent_incidents(limit: int = 20) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_incidents() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
