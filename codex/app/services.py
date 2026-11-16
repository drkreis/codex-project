from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app import db
from app.db import GOLDEN_DIR, STANDARDS_DIR, get_db, init_db
from app.errors import UserError
from app.parameters import PARAMETERS, SPECS, STATUS_LABELS, UNITS, bounds, label, parse_params, quantity, read_params, validate_quantity
from app.parser import KEY_FIELDS, compare_to_golden, fields_to_record, parse_document, score_batch
from app.parser.accuracy import load_golden
from app.parser.extract import MAX_DOCUMENT_BYTES

EDITABLE = ("document_id", "title", "product_name", "constraints_text", "notes", "status")
SNAPSHOT_FIELDS = ("id", "document_id", "title", "product_name", "constraints_text", "notes", "status", "params", "revision")
MAX_SELECTION = 12


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def log_change(conn, entity: str, entity_id: int, field_name: str, old: str | None, new: str | None, user: str = "operator") -> None:
    if old == new:
        return
    conn.execute(
        "INSERT INTO change_log(entity,entity_id,field_name,old_value,new_value,user_name) VALUES (?,?,?,?,?,?)",
        (entity, entity_id, field_name, old, new, user),
    )


def decorate_record(row) -> dict[str, Any]:
    data = dict(row)
    data["params"], data["params_error"] = read_params(data.get("params_json"))
    data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
    try:
        fields = json.loads(data.get("manual_fields_json") or "[]")
        if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
            raise ValueError("Invalid protection metadata")
        data["manual_fields"] = fields
    except (ValueError, TypeError):
        data["manual_fields"] = [*EDITABLE, "params_json"]
    return data


def parsed_record(parsed: dict[str, Any]) -> dict[str, Any]:
    record = fields_to_record(parsed)
    record["params_json"] = dumps(parse_params(record["params_json"]))
    return record


def _record_changes(conn, record_id: int, old: dict, new: dict, user: str) -> list[str]:
    changed = []
    for field in EDITABLE:
        if old.get(field) != new.get(field):
            changed.append(field)
            log_change(conn, "records", record_id, field, old.get(field), new.get(field), user)
    old_params, old_error = read_params(old.get("params_json"))
    new_params, new_error = read_params(new.get("params_json"))
    if old_error or new_error:
        if old.get("params_json") != new.get("params_json"):
            changed.append("params_json")
            log_change(conn, "records", record_id, "params_json", old.get("params_json"), new["params_json"], user)
    else:
        for key in sorted(old_params.keys() | new_params.keys()):
            if old_params.get(key) != new_params.get(key):
                changed.append("params." + key)
                log_change(conn, "records", record_id, "params." + key, old_params.get(key), new_params.get(key), user)
    return changed


def upsert_parsed(conn, parsed: dict[str, Any]) -> tuple[int, int]:
    document_id = parsed["document_id"]
    basename = Path(parsed.get("file_path") or "").name.casefold()
    standards = conn.execute("SELECT * FROM standards").fetchall()
    by_file = next((r for r in standards if basename and Path(r["file_path"] or "").name.casefold() == basename), None)
    by_id = next((r for r in standards if r["document_id"] == document_id), None)
    if by_file and by_id and by_file["id"] != by_id["id"]:
        raise UserError("Имя файла и обозначение относятся к разным документам. Переименуйте файл.", "file")
    existing = by_file or by_id
    if existing:
        standard_id = existing["id"]
        conn.execute(
            """UPDATE standards SET document_id=?,title=?,file_path=?,format=?,source_text=?,
               status='parsed',loaded_at=datetime('now') WHERE id=?""",
            (document_id, parsed["title"], parsed.get("file_path"), parsed.get("format", "txt"), parsed.get("source_text"), standard_id),
        )
        conn.execute("DELETE FROM extracted_fields WHERE standard_id=?", (standard_id,))
    else:
        cur = conn.execute(
            "INSERT INTO standards(document_id,title,file_path,format,source_text,status) VALUES (?,?,?,?,?,'parsed')",
            (document_id, parsed["title"], parsed.get("file_path"), parsed.get("format", "txt"), parsed.get("source_text")),
        )
        standard_id = cur.lastrowid
    for key in KEY_FIELDS:
        conn.execute(
            "INSERT INTO extracted_fields(standard_id,field_key,value,confidence,source) VALUES (?,?,?,?,'parser')",
            (standard_id, key, str(parsed.get(key, "")), float(parsed.get("confidences", {}).get(key, .5))),
        )
    incoming = parsed_record(parsed)
    # The relation survives an operator's edits to the visible document ID.
    row = conn.execute("SELECT * FROM records WHERE standard_id=? ORDER BY id LIMIT 1", (standard_id,)).fetchone()
    if row:
        current = decorate_record(row)
        protected = set(current["manual_fields"])
        merged = {f: current[f] if f in protected else incoming[f] for f in EDITABLE}
        params = parse_params(incoming["params_json"])
        if "params_json" in protected or current["params_error"]:
            merged["params_json"] = current["params_json"]
        else:
            for field in protected:
                if field.startswith("params."):
                    key = field[7:]
                    if key in current["params"]:
                        params[key] = current["params"][key]
                    else:
                        params.pop(key, None)
            merged["params_json"] = dumps(params)
        record_id = current["id"]
        changes = _record_changes(conn, record_id, current, merged, "parser")
        if changes or merged["params_json"] != current["params_json"]:
            conn.execute(
                """UPDATE records SET document_id=?,title=?,product_name=?,constraints_text=?,notes=?,status=?,
                   params_json=?,updated_at=datetime('now'),revision=revision+1 WHERE id=?""",
                (*[merged[f] for f in EDITABLE], merged["params_json"], record_id),
            )
    else:
        cur = conn.execute(
            """INSERT INTO records(standard_id,document_id,title,product_name,constraints_text,notes,status,params_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (standard_id, *[incoming[f] for f in EDITABLE], incoming["params_json"]),
        )
        record_id = cur.lastrowid
        log_change(conn, "records", record_id, "create", None, "Загружено из документа", "parser")
    return standard_id, record_id


def ingest_file(path: Path) -> dict[str, Any]:
    parsed = parse_document(path)
    with get_db() as conn:
        standard_id, record_id = upsert_parsed(conn, parsed)
    return {"standard_id": standard_id, "record_id": record_id, "document_id": parsed["document_id"]}


def ingest_directory(directory: Path | None = None) -> list[dict[str, Any]]:
    results = []
    with get_db() as conn:
        active_sources = {r["document_id"]: Path(r["file_path"] or "").name.casefold()
                          for r in conn.execute("SELECT document_id,file_path FROM standards")}
    for path in sorted((directory or STANDARDS_DIR).glob("*")):
        if path.suffix.lower() not in {".txt", ".pdf", ".md"}:
            continue
        try:
            parsed = parse_document(path)
            active = active_sources.get(parsed["document_id"])
            if active and active != path.name.casefold():
                continue  # An older file must not replace the active uploaded version.
            with get_db() as conn:
                standard_id, record_id = upsert_parsed(conn, parsed)
            active_sources[parsed["document_id"]] = path.name.casefold()
            results.append({"standard_id": standard_id, "record_id": record_id, "document_id": parsed["document_id"]})
        except (UserError, OSError) as exc:
            results.append({"filename": path.name, "error": str(exc)})
    return results


def import_upload(filename: str, data: bytes) -> dict[str, Any]:
    filename = Path(filename.replace("\\", "/")).name
    if not filename or filename.startswith(".") or len(filename) > 160 or any(c in filename for c in '<>:"|?*'):
        raise UserError("Укажите обычное имя файла без специальных символов.", "file")
    if filename.endswith((" ", ".")) or filename.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise UserError("Это имя зарезервировано Windows. Переименуйте документ.", "file")
    if Path(filename).suffix.lower() not in {".txt", ".pdf", ".md"}:
        raise UserError("Этот формат не поддерживается. Выберите TXT, MD или текстовый PDF.", "file")
    if not data:
        raise UserError("Файл пуст. Выберите документ с текстом.", "file")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise UserError("Документ слишком большой. Максимальный размер — 10 МБ.", "file")
    with tempfile.TemporaryDirectory(prefix="codex-import-") as temp_dir:
        staged = Path(temp_dir) / filename
        staged.write_bytes(data)
        parsed = parse_document(staged)
    STANDARDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = STANDARDS_DIR / filename
    if dest.is_symlink() or dest.resolve().parent != STANDARDS_DIR.resolve():
        raise UserError("Нельзя заменить этот файл. Выберите другое имя документа.", "file")
    parsed["file_path"] = str(dest)
    with db.DB_LOCK:
        previous = dest.read_bytes() if dest.exists() else None
        staging = dest.with_name(dest.name + ".pending")
        try:
            with get_db() as conn:
                standard_id, record_id = upsert_parsed(conn, parsed)
                staging.write_bytes(data)
                os.replace(staging, dest)
        except Exception:
            if previous is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_bytes(previous)
            raise
        finally:
            staging.unlink(missing_ok=True)
    return {"standard_id": standard_id, "record_id": record_id, "document_id": parsed["document_id"]}


def list_records(q: str | None = None, parameter: str = "", minimum: str = "", maximum: str = "", status: str = "") -> list[dict[str, Any]]:
    lo, hi = bounds(parameter, minimum, maximum)
    with get_db() as conn:
        rows = [decorate_record(r) for r in conn.execute("SELECT * FROM records ORDER BY id")]
    if q and q.strip():
        needle = q.strip().casefold()
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in
                ("id", "document_id", "title", "product_name", "params_json", "constraints_text", "notes")).casefold()]
    if status:
        if status not in STATUS_LABELS:
            raise UserError("Выберите статус из списка.", "status")
        rows = [r for r in rows if r["status"] == status]
    if lo is not None or hi is not None:
        rows = [r for r in rows if (value := quantity(r["params"].get(parameter, ""), parameter)) is not None
                and (lo is None or value[0] >= lo) and (hi is None or value[1] <= hi)]
    return rows


def get_record(record_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return None
        data = decorate_record(row)
        data["extracted_fields"] = [dict(f) for f in conn.execute(
            "SELECT field_key,value,confidence,source FROM extracted_fields WHERE standard_id=?", (data["standard_id"],),
        )]
    source = {f["field_key"]: f["value"] for f in data["extracted_fields"]}
    parsed_params, _ = read_params(source.get("params"))
    conflicts = []
    for field in data["manual_fields"]:
        if field.startswith("params."):
            key = field[7:]
            manual, automatic = data["params"].get(key), parsed_params.get(key)
        elif field == "params_json":
            manual = "; ".join(f"{label(k)}: {v}" for k, v in data["params"].items())
            automatic = "; ".join(f"{label(k)}: {v}" for k, v in parsed_params.items())
        else:
            manual = data.get(field)
            automatic = source.get("constraints" if field == "constraints_text" else field)
            if field == "status":
                continue
        if manual != automatic:
            conflicts.append({"label": label(field), "manual": manual, "parsed": automatic})
    data["conflicts"] = conflicts
    return data


def validate_record(payload: dict[str, Any], current: dict | None = None) -> dict[str, Any]:
    result = {}
    limits = {"document_id": 150, "title": 500, "product_name": 300, "constraints_text": 5000, "notes": 5000, "status": 30}
    for field in EDITABLE:
        value = str(payload.get(field, current.get(field, "") if current else "") or "").strip()
        if field in ("document_id", "product_name") and not value:
            raise UserError(f"Заполните поле «{label(field)}».", field)
        if len(value) > limits[field]:
            raise UserError(f"В поле «{label(field)}» допускается не более {limits[field]} символов.", field)
        result[field] = value
    if result["status"] not in STATUS_LABELS:
        raise UserError("Выберите статус из списка.", "status")
    params = parse_params(payload.get("params", current["params"] if current else {}))
    for key, value in params.items():
        if key in UNITS:
            validate_quantity(value, key)
    result["params_json"] = dumps(params)
    return result


def update_record(record_id: int, payload: dict[str, Any], user: str = "operator") -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return None
        current = decorate_record(row)
        expected = payload.get("revision")
        if expected is not None and str(expected) != str(current["revision"]):
            raise UserError("Запись уже изменилась в другой вкладке или при импорте. Ваш ввод остался в форме; откройте актуальную карточку перед повторной правкой.", "", 409)
        mapping = validate_record(payload, current)
        changes = _record_changes(conn, record_id, current, mapping, user)
        protected = sorted(set(current["manual_fields"]) | set(changes))
        if changes:
            conn.execute(
                """UPDATE records SET document_id=?,title=?,product_name=?,constraints_text=?,notes=?,status=?,
                   params_json=?,manual_fields_json=?,updated_at=datetime('now'),revision=revision+1 WHERE id=?""",
                (*[mapping[f] for f in EDITABLE], mapping["params_json"], dumps(protected), record_id),
            )
    return get_record(record_id)


def create_record(payload: dict[str, Any], user: str = "operator") -> dict[str, Any]:
    mapping = validate_record({"status": "manual", **payload})
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO records(document_id,title,product_name,constraints_text,notes,status,params_json)
               VALUES (?,?,?,?,?,?,?)""",
            (*[mapping[f] for f in EDITABLE], mapping["params_json"]),
        )
        record_id = cur.lastrowid
        log_change(conn, "records", record_id, "create", None, "Введено вручную", user)
    return get_record(record_id)


def delete_record(record_id: int, user: str = "operator") -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return False
        log_change(conn, "records", record_id, "delete", None, dumps(dict(row)), user)
        conn.execute("DELETE FROM records WHERE id=?", (record_id,))
    return True


def list_history(record_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with get_db() as conn:
        sql = """SELECT c.*,r.product_name FROM change_log c
                 LEFT JOIN records r ON c.entity='records' AND r.id=c.entity_id"""
        args = []
        if record_id is not None:
            sql += " WHERE c.entity='records' AND c.entity_id=?"
            args.append(record_id)
        sql += " ORDER BY c.id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, [*args, limit])]


def selected_records(record_ids: list[int]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(record_ids))
    if not ids:
        raise UserError("Отметьте хотя бы одно изделие в списке записей.", "record_ids")
    if len(ids) > MAX_SELECTION:
        raise UserError(f"За один раз можно сравнить до {MAX_SELECTION} изделий.", "record_ids")
    with get_db() as conn:
        rows = {r["id"]: decorate_record(r) for r in conn.execute(
            "SELECT * FROM records WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
        )}
    missing = [str(i) for i in ids if i not in rows]
    if missing:
        raise UserError("Эти записи уже удалены: " + ", ".join(missing) + ". Обновите выбранные изделия.", "record_ids", 409)
    return [rows[i] for i in ids]


def save_selection(query: str, record_ids: list[int], note: str = "", expected_revisions: dict | None = None) -> int:
    query, note = query.strip(), note.strip()
    if not query or len(query) > 300:
        raise UserError("Укажите название подбора длиной от 1 до 300 символов.", "query")
    if len(note) > 5000:
        raise UserError("Заметка должна быть короче 5000 символов.", "note")
    with db.DB_LOCK:
        records = selected_records(record_ids)
        if expected_revisions is not None and any(str(expected_revisions.get(str(r["id"]))) != str(r["revision"]) for r in records):
            raise UserError("Характеристики изменились после открытия сравнения. Проверьте обновлённые значения и сохраните подбор ещё раз.", "", 409)
        snapshots = [{k: r[k] for k in SNAPSHOT_FIELDS} for r in records]
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO selection_results(query,record_ids,note,records_snapshot_json) VALUES (?,?,?,?)",
                (query, dumps([r["id"] for r in records]), note, dumps(snapshots)),
            )
            return cur.lastrowid


def list_selections() -> list[dict[str, Any]]:
    with get_db() as conn:
        out = []
        for row in conn.execute("SELECT * FROM selection_results ORDER BY id DESC"):
            item = dict(row)
            try:
                item["record_ids"] = json.loads(item["record_ids"] or "[]")
            except ValueError:
                item["record_ids"] = []
            item["count"] = len(item["record_ids"])
            out.append(item)
    return out


def get_selection(selection_id: int) -> dict[str, Any] | None:
    selection = next((s for s in list_selections() if s["id"] == selection_id), None)
    if selection is None:
        return None
    selection["legacy"] = not selection["records_snapshot_json"]
    if selection["legacy"]:
        with get_db() as conn:
            rows = {r["id"]: decorate_record(r) for r in conn.execute("SELECT * FROM records")}
        selection["records"] = [rows[i] for i in selection["record_ids"] if i in rows]
        selection["missing"] = len(selection["record_ids"]) - len(selection["records"])
    else:
        selection["records"] = json.loads(selection["records_snapshot_json"])
        selection["missing"] = 0
    for r in selection["records"]:
        r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
    return selection


def comparison_rows(records: list[dict]) -> list[dict]:
    keys = list(dict.fromkeys([p.key for p in PARAMETERS if any(p.key in r["params"] for r in records)] +
                             [k for r in records for k in r["params"]]))
    rows = [{"label": label(k), "values": [r["params"].get(k, "—") for r in records]} for k in keys]
    rows += [{"label": label(k), "values": [r.get(k) or "—" for r in records]} for k in ("constraints_text", "notes")]
    for row in rows:
        row["different"] = len(set(row["values"])) > 1
    return rows


def selection_csv(selection: dict) -> bytes:
    records = selection["records"]
    keys = list(dict.fromkeys(k for r in records for k in r["params"]))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
    def safe(value):
        text = str(value or "")
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text
    writer.writerow(["Подбор", "Сохранён", "Заметка к подбору", "ID", "Документ", "Название документа", "Изделие",
                     *[label(k) for k in keys], "Условия применения", "Примечание", "Статус"])
    for r in records:
        writer.writerow([safe(v) for v in (selection["query"], selection["exported_at"], selection["note"], r["id"],
                         r["document_id"], r["title"], r["product_name"], *[r["params"].get(k, "") for k in keys],
                         r["constraints_text"], r["notes"], r["status_label"])])
    return stream.getvalue().encode("utf-8-sig")


def status_snapshot() -> dict[str, Any]:
    with get_db() as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("standards", "records", "extracted_fields", "selection_results", "change_log")}
        manual_count = conn.execute("SELECT COUNT(*) FROM records WHERE manual_fields_json != '[]'").fetchone()[0]
    return {
        "standards_count": counts["standards"], "records_count": counts["records"],
        "extracted_fields_count": counts["extracted_fields"], "selections_count": counts["selection_results"],
        "change_log_count": counts["change_log"], "protected_count": manual_count,
        "target_standards": 10, "target_accuracy": .95, "accuracy": evaluate_accuracy(),
    }


def evaluate_accuracy() -> dict[str, Any]:
    pairs, details, skipped = [], [], []
    for golden_path in sorted(GOLDEN_DIR.glob("*.json")):
        std_path = next((STANDARDS_DIR / f"{golden_path.stem}{ext}" for ext in (".txt", ".pdf", ".md")
                         if (STANDARDS_DIR / f"{golden_path.stem}{ext}").exists()), None)
        if std_path is None:
            skipped.append({"filename": golden_path.name, "error": "Исходный документ не найден"})
            continue
        try:
            golden = load_golden(golden_path)
            predicted = parse_document(std_path)
            one = compare_to_golden(predicted, golden)
        except (UserError, OSError, ValueError) as exc:
            skipped.append({"filename": golden_path.name, "error": str(exc)})
            continue
        one["document_id"] = golden.get("document_id", golden_path.stem)
        details.append(one)
        pairs.append((predicted, golden))
    summary = score_batch(pairs)
    summary.update(per_document=details, skipped=skipped)
    return summary


def bootstrap() -> dict[str, Any]:
    init_db()
    results = ingest_directory()
    return {"ingested": sum("error" not in r for r in results), "errors": [r for r in results if "error" in r], "status": status_snapshot()}
