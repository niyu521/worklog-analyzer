"""Event store schema + connection helper for Company Brain Step1."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "events.db"
BLOBS_DIR = Path(__file__).resolve().parent.parent / "data" / "blobs"

DDL = """
CREATE TABLE IF NOT EXISTS episodes (
    master_id    TEXT PRIMARY KEY,
    task_label   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    master_id       TEXT NOT NULL REFERENCES episodes(master_id),
    parent_event_id TEXT REFERENCES events(event_id),
    platform        TEXT NOT NULL,
    native_id       TEXT,
    event_type      TEXT NOT NULL,
    content_hash    TEXT,
    content_ref     TEXT,
    metadata_json   TEXT,
    captured_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_native ON events(platform, native_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_events_master ON events(master_id, captured_at);
"""


def get_conn():
    BLOBS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(DDL)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
