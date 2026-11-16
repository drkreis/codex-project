from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import db
from app.errors import UserError
from app.parameters import parse_params

MAX_BACKUP_BYTES = 50 * 1024 * 1024
REQUIRED = {
    "standards": {"id", "document_id", "title", "file_path", "format", "source_text", "loaded_at", "status"},
    "extracted_fields": {"id", "standard_id", "field_key", "value", "confidence", "source"},
    "records": {"id", "standard_id", "document_id", "title", "product_name", "params_json", "constraints_text", "notes", "status", "created_at", "updated_at"},
    "selection_results": {"id", "query", "record_ids", "note", "exported_at"},
    "change_log": {"id", "entity", "entity_id", "field_name", "old_value", "new_value", "user_name", "ts"},
}


def create_backup(reason: str = "manual") -> Path:
    db.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    dest = db.BACKUPS_DIR / f"codex-{stamp}-{reason}-{uuid4().hex[:8]}.db"
    try:
        with db.DB_LOCK, db.get_db() as source, closing(sqlite3.connect(dest)) as target:
            source.backup(target)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


def list_backups() -> list[dict]:
    if not db.BACKUPS_DIR.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(db.BACKUPS_DIR.glob("codex-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)]


def backup_path(name: str) -> Path:
    if name != Path(name).name or not name.startswith("codex-") or not name.endswith(".db"):
        raise UserError("Резервная копия не найдена.", "", 404)
    path = db.BACKUPS_DIR / name
    if not path.is_file() or path.is_symlink() or path.resolve().parent != db.BACKUPS_DIR.resolve():
        raise UserError("Резервная копия не найдена.", "", 404)
    return path


def validate_backup(path: Path) -> dict[str, int]:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            if conn.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise UserError("Эта копия создана более новой версией программы.", "file")
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise UserError("Резервная копия повреждена.", "file")
            objects = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if any(kind in ("trigger", "view") or (sql and "VIRTUAL TABLE" in sql.upper()) for kind, _, sql in objects):
                raise UserError("Это не резервная копия Codex: в ней есть посторонние объекты.", "file")
            tables = {name for kind, name, _ in objects if kind == "table"}
            if tables != set(REQUIRED):
                raise UserError("Выберите базу Codex. Набор таблиц в этом файле не совпадает.", "file")
            for table, required in REQUIRED.items():
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise UserError(f"В резервной копии неполная таблица {table}.", "file")
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise UserError("В резервной копии нарушены связи между документами и записями.", "file")
            for (value,) in conn.execute("SELECT params_json FROM records"):
                parse_params(value)
            for (value,) in conn.execute("SELECT record_ids FROM selection_results"):
                ids = json.loads(value)
                if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
                    raise UserError("В копии повреждены сохранённые подборы.", "file")
            selection_columns = {r[1] for r in conn.execute("PRAGMA table_info(selection_results)")}
            if "records_snapshot_json" in selection_columns:
                for (value,) in conn.execute("SELECT records_snapshot_json FROM selection_results WHERE records_snapshot_json IS NOT NULL"):
                    records = json.loads(value)
                    if not isinstance(records, list):
                        raise UserError("В копии повреждены характеристики подбора.", "file")
                    for r in records:
                        if not isinstance(r, dict) or not {"id", "document_id", "title", "product_name", "params", "constraints_text", "notes", "status"} <= r.keys():
                            raise UserError("В копии неполные данные подбора.", "file")
                        if type(r["id"]) is not int or r["id"] <= 0 or any(not isinstance(r[k], str) for k in ("document_id", "title", "product_name", "constraints_text", "notes", "status")):
                            raise UserError("В копии неверный формат данных подбора.", "file")
                        parse_params(r["params"])
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in REQUIRED}
    except UserError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise UserError("Не удалось проверить копию. Выберите неповреждённый файл базы Codex (.db).", "file") from exc


def restore_backup(data: bytes) -> Path:
    if not data or len(data) > MAX_BACKUP_BYTES or not data.startswith(b"SQLite format 3\x00"):
        raise UserError("Выберите резервную копию SQLite (.db) размером до 50 МБ.", "file")
    with tempfile.TemporaryDirectory(prefix="codex-restore-") as temp_dir:
        candidate = Path(temp_dir) / "restore.db"
        candidate.write_bytes(data)
        validate_backup(candidate)
        # Migrate and check a disposable copy before touching the active DB.
        with db.DB_LOCK:
            original = db.DB_PATH
            try:
                db.DB_PATH = candidate
                db.init_db()
            finally:
                db.DB_PATH = original
            validate_backup(candidate)
            safety_copy = create_backup("before-restore")
            try:
                with closing(sqlite3.connect(candidate)) as source, db.get_db() as target:
                    source.backup(target)
            except Exception:
                with closing(sqlite3.connect(safety_copy)) as source, db.get_db() as target:
                    source.backup(target)
                raise
    return safety_copy
