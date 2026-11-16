from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CODEX_DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "codex.db"
STANDARDS_DIR = DATA_DIR / "standards"
GOLDEN_DIR = DATA_DIR / "golden"
UPLOADS_DIR = DATA_DIR / "uploads"
BACKUPS_DIR = DATA_DIR / "backups"
DB_LOCK = RLock()

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS standards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    file_path TEXT,
    format TEXT NOT NULL DEFAULT 'txt',
    source_text TEXT,
    loaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'loaded'
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_id INTEGER NOT NULL REFERENCES standards(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'parser',
    UNIQUE(standard_id, field_key)
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_id INTEGER REFERENCES standards(id) ON DELETE SET NULL,
    document_id TEXT,
    title TEXT,
    product_name TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    constraints_text TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    manual_fields_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS selection_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    record_ids TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    records_snapshot_json TEXT,
    exported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    user_name TEXT NOT NULL DEFAULT 'operator',
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_records_product ON records(product_name);
CREATE INDEX IF NOT EXISTS idx_records_document ON records(document_id);
CREATE INDEX IF NOT EXISTS idx_fields_standard ON extracted_fields(standard_id);
CREATE INDEX IF NOT EXISTS idx_changes_entity ON change_log(entity, entity_id, id);
"""


def ensure_dirs() -> None:
    for path in (DATA_DIR, STANDARDS_DIR, GOLDEN_DIR, UPLOADS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    with DB_LOCK:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    ensure_dirs()
    with get_db() as conn:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        if "manual_fields_json" not in columns:
            conn.execute("ALTER TABLE records ADD COLUMN manual_fields_json TEXT NOT NULL DEFAULT '[]'")
            # Protect edits already recorded by previous versions as well.
            for row in conn.execute("SELECT id FROM records").fetchall():
                fields = [r[0] for r in conn.execute(
                    """SELECT DISTINCT field_name FROM change_log
                       WHERE entity='records' AND entity_id=? AND user_name != 'parser'
                       AND field_name IN ('document_id','title','product_name','params_json','constraints_text','notes','status')""",
                    (row["id"],),
                )]
                conn.execute("UPDATE records SET manual_fields_json=? WHERE id=?", (json.dumps(fields), row["id"]))
        if "revision" not in columns:
            conn.execute("ALTER TABLE records ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        selection_columns = {r["name"] for r in conn.execute("PRAGMA table_info(selection_results)")}
        if "records_snapshot_json" not in selection_columns:
            conn.execute("ALTER TABLE selection_results ADD COLUMN records_snapshot_json TEXT")
        conn.execute("PRAGMA user_version = 2")
