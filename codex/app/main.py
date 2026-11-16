from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app import backups, services
from app.db import ROOT, get_db, init_db
from app.errors import UserError
from app.parameters import PARAMETERS, SPECS, STATUS_LABELS, UNITS, canonical_key, label, parse_params, validate_quantity
from app.parser.extract import MAX_DOCUMENT_BYTES

@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_startup)
    yield


app = FastAPI(title="Codex", version="0.2.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates.env.filters["field_label"] = label
templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(value, value)


def local_date(value):
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return value or "—"


templates.env.filters["local_date"] = local_date
NOTICES = {
    "record_saved": "Запись сохранена. Ручные правки защищены при повторном разборе документа.",
    "record_created": "Запись создана.",
    "record_deleted": "Запись удалена. Сохранённые подборы с её характеристиками остались доступны.",
    "selection_saved": "Подбор сохранён вместе с характеристиками изделий.",
    "backup_created": "Резервная копия создана. Её можно скачать на компьютер.",
    "restored": "База восстановлена. Предыдущее состояние сохранено в резервных копиях.",
    "uploaded": "Документ загружен и разобран. Ручные правки сохранены.",
}


def _startup() -> None:
    init_db()
    with get_db() as conn:
        empty = conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0] == 0
    if empty:
        services.ingest_directory()


def _ctx(request: Request, **extra):
    return {
        "request": request, "status": services.status_snapshot(), "parameters": PARAMETERS,
        "numeric_parameters": [p for p in PARAMETERS if p.key in UNITS], "status_labels": STATUS_LABELS,
        "notice": NOTICES.get(request.query_params.get("notice", ""), ""),
        "error": None, **extra,
    }


def render(request: Request, name: str, status_code: int = 200, **extra):
    return templates.TemplateResponse(request=request, name=name, context=_ctx(request, **extra), status_code=status_code)


def redirect(url: str, notice: str = ""):
    return RedirectResponse(url + (("?notice=" + notice) if notice else ""), status_code=303)


@app.exception_handler(UserError)
async def user_error(request: Request, exc: UserError):
    return render(request, "error.html", exc.status_code, error=exc.message)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logging.getLogger("codex").exception("Request failed: %s", request.url.path, exc_info=exc)
    return render(request, "error.html", 500, error="Не удалось выполнить операцию. Попробуйте ещё раз или вернитесь к списку записей.")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", recent=services.list_history(limit=6))


def records_response(request: Request, filters: dict | None = None, error: UserError | None = None):
    filters = filters or {k: request.query_params.get(k, "") for k in ("q", "parameter", "minimum", "maximum", "status")}
    try:
        rows = services.list_records(**filters)
    except UserError as exc:
        error, rows = exc, []
    criteria = filters["q"].strip()
    if filters["parameter"] in SPECS and (filters["minimum"] or filters["maximum"]):
        p = SPECS[filters["parameter"]]
        criteria += ("; " if criteria else "") + f"{p.label} ({p.unit}): от {filters['minimum'] or '…'} до {filters['maximum'] or '…'}"
    return render(request, "records.html", error.status_code if error else 200,
                  records=rows, filters=filters, criteria=criteria, error=error.message if error else None)


@app.get("/records", response_class=HTMLResponse)
def records_list(request: Request, q: str | None = None):
    filters = {k: request.query_params.get(k, "") for k in ("q", "parameter", "minimum", "maximum", "status")}
    if q is not None:
        filters["q"] = q
    return records_response(request, filters)


def form_response(request: Request, record=None, form=None, error: UserError | None = None):
    values = {f: record.get(f, "") if record else "" for f in services.EDITABLE}
    values["status"] = record["status"] if record else "manual"
    params = dict(record["params"]) if record else {}
    custom = [(k, v) for k, v in params.items() if k not in SPECS]
    legacy_params = None
    if form is not None:
        values.update({f: form.get(f, "") for f in services.EDITABLE})
        if "params" in form:
            legacy_params = form.get("params", "")
            try:
                params = parse_params(legacy_params)
                custom = [(k, v) for k, v in params.items() if k not in SPECS]
                legacy_params = None
            except UserError:
                pass
        else:
            params = {p.key: form.get("param_" + p.key, "") for p in PARAMETERS}
            custom = list(zip(form.getlist("extra_key"), form.getlist("extra_value")))
    revision = form.get("revision", "") if form is not None else str(record["revision"]) if record else ""
    return render(request, "record_form.html", error.status_code if error else 200,
                  record=record, mode="edit" if record else "create", values=values, param_values=params,
                  custom_params=custom or [("", "")], revision=revision, legacy_params=legacy_params,
                  error=error.message if error else None, error_field=error.field if error else "")


def payload_from_form(form):
    result = {f: form.get(f, "") for f in services.EDITABLE}
    if "revision" in form:
        result["revision"] = form["revision"]
    if "params" in form:
        result["params"] = form["params"]
        return result
    params = {p.key: form.get("param_" + p.key, "").strip() for p in PARAMETERS if form.get("param_" + p.key, "").strip()}
    for key, value in params.items():
        if key in UNITS:
            validate_quantity(value, key)
    keys, values = form.getlist("extra_key"), form.getlist("extra_value")
    if len(keys) != len(values) or len(keys) > 50:
        raise UserError("Проверьте строки дополнительных характеристик.", "params")
    for key, value in zip(keys, values):
        if not key.strip() and not value.strip():
            continue
        if not key.strip() or not value.strip():
            raise UserError("В дополнительной характеристике заполните и название, и значение.", "params")
        key = canonical_key(key)
        if key in params:
            raise UserError(f"Характеристика «{label(key)}» указана дважды.", "params")
        params[key] = value
    result["params"] = params
    return result


@app.get("/records/new", response_class=HTMLResponse)
def record_new_form(request: Request):
    return form_response(request)


@app.post("/records/new")
async def record_create(request: Request):
    form = await request.form()
    try:
        record = await run_in_threadpool(services.create_record, payload_from_form(form))
    except UserError as exc:
        return form_response(request, form=form, error=exc)
    return redirect(f"/records/{record['id']}", "record_created")


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_view(request: Request, record_id: int):
    record = services.get_record(record_id)
    if record is None:
        raise UserError("Запись не найдена. Возможно, её уже удалили.", "", 404)
    return render(request, "record_view.html", record=record, history=services.list_history(record_id))


@app.get("/records/{record_id}/edit", response_class=HTMLResponse)
def record_edit_form(request: Request, record_id: int):
    record = services.get_record(record_id)
    if record is None:
        raise UserError("Запись не найдена.", "", 404)
    return form_response(request, record)


@app.post("/records/{record_id}/edit")
async def record_edit(request: Request, record_id: int):
    form = await request.form()
    current = await run_in_threadpool(services.get_record, record_id)
    if current is None:
        raise UserError("Запись уже удалена. Вернитесь к списку.", "", 404)
    try:
        await run_in_threadpool(services.update_record, record_id, payload_from_form(form))
    except UserError as exc:
        return form_response(request, current, form, exc)
    return redirect(f"/records/{record_id}", "record_saved")


@app.post("/records/{record_id}/delete")
def record_delete(record_id: int):
    if not services.delete_record(record_id):
        raise UserError("Запись уже удалена.", "", 404)
    return redirect("/records", "record_deleted")


def standards_response(request, error=None, results=None):
    with get_db() as conn:
        standards = [dict(r) for r in conn.execute("SELECT * FROM standards ORDER BY id DESC")]
    return render(request, "standards.html", error.status_code if error else 200, standards=standards,
                  error=error.message if error else None, import_results=results)


@app.get("/standards", response_class=HTMLResponse)
def standards_page(request: Request):
    return standards_response(request)


@app.post("/standards/upload")
async def standards_upload(request: Request, file: UploadFile = File(...)):
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    try:
        await run_in_threadpool(services.import_upload, file.filename or "", data)
    except UserError as exc:
        return standards_response(request, exc)
    return redirect("/standards", "uploaded")


@app.post("/standards/reparse")
def standards_reparse(request: Request):
    return standards_response(request, results=services.ingest_directory())


@app.get("/standards/{standard_id}/text")
def standard_text(standard_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT source_text FROM standards WHERE id=?", (standard_id,)).fetchone()
    if row is None:
        raise UserError("Документ не найден.", "", 404)
    return Response(content=row["source_text"] or "", media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="document-{standard_id}.txt"'})


@app.get("/accuracy", response_class=HTMLResponse)
def accuracy_page(request: Request):
    return render(request, "accuracy.html", metrics=services.evaluate_accuracy())


@app.get("/selections", response_class=HTMLResponse)
def selections_page(request: Request):
    return render(request, "selections.html", selections=services.list_selections())


def form_ids(form) -> list[int]:
    ids = []
    for value in form.getlist("record_ids"):
        for part in value.replace(";", ",").split(","):
            if not part.strip().isdigit() or int(part) <= 0:
                raise UserError("Проверьте выбранные изделия.", "record_ids")
            ids.append(int(part))
    return list(dict.fromkeys(ids))


def comparison_response(request, records, query="", note="", error=None):
    return render(request, "comparison.html", error.status_code if error else 200,
                  records=records, comparison=services.comparison_rows(records), selection=None,
                  query=query or "Подбор изделий", note=note,
                  revisions=services.dumps({str(r["id"]): r["revision"] for r in records}),
                  error=error.message if error else None)


@app.post("/selections/preview")
async def selection_preview(request: Request):
    form = await request.form()
    try:
        records = await run_in_threadpool(services.selected_records, form_ids(form))
    except UserError as exc:
        return records_response(request, {k: "" for k in ("q", "parameter", "minimum", "maximum", "status")}, exc)
    return comparison_response(request, records, form.get("query", ""))


@app.post("/selections")
async def selections_create(request: Request):
    form = await request.form()
    records = []
    try:
        ids = form_ids(form)
        records = await run_in_threadpool(services.selected_records, ids)
        expected = json.loads(form.get("revisions", "{}"))
        if not isinstance(expected, dict):
            raise UserError("Обновите страницу сравнения.", "", 409)
        selection_id = await run_in_threadpool(services.save_selection, form.get("query", ""), ids, form.get("note", ""), expected)
    except json.JSONDecodeError:
        return comparison_response(request, records, form.get("query", ""), form.get("note", ""),
                                   UserError("Обновите страницу сравнения.", "", 409))
    except UserError as exc:
        if not records:
            return records_response(request, {k: "" for k in ("q", "parameter", "minimum", "maximum", "status")}, exc)
        return comparison_response(request, records, form.get("query", ""), form.get("note", ""), exc)
    return redirect(f"/selections/{selection_id}", "selection_saved")


@app.get("/selections/{selection_id}", response_class=HTMLResponse)
def selection_view(request: Request, selection_id: int):
    selection = services.get_selection(selection_id)
    if selection is None:
        raise UserError("Подбор не найден.", "", 404)
    return render(request, "comparison.html", selection=selection, records=selection["records"],
                  comparison=services.comparison_rows(selection["records"]))


@app.get("/selections/{selection_id}/export.csv")
def selection_export(selection_id: int):
    selection = services.get_selection(selection_id)
    if selection is None:
        raise UserError("Подбор не найден.", "", 404)
    return Response(services.selection_csv(selection), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="selection-{selection_id}.csv"'})


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, record_id: int | None = None):
    return render(request, "history.html", history=services.list_history(record_id, 500), record_id=record_id)


def backups_response(request, error=None):
    return render(request, "backups.html", error.status_code if error else 200,
                  backups=backups.list_backups(), error=error.message if error else None)


@app.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request):
    return backups_response(request)


@app.post("/backups")
def backup_create():
    backups.create_backup()
    return redirect("/backups", "backup_created")


@app.get("/backups/{name}")
def backup_download(name: str):
    path = backups.backup_path(name)
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)


@app.post("/backups/restore")
async def backup_restore(request: Request, file: UploadFile = File(...)):
    form = await request.form()
    try:
        if form.get("confirm") != "yes":
            raise UserError("Подтвердите замену текущей базы выбранной копией.", "confirm")
        data = await file.read(backups.MAX_BACKUP_BYTES + 1)
        await run_in_threadpool(backups.restore_backup, data)
    except UserError as exc:
        return backups_response(request, exc)
    return redirect("/backups", "restored")


@app.get("/api/status")
def api_status():
    return services.status_snapshot()


@app.get("/api/records")
def api_records(q: str | None = None, parameter: str = "", minimum: str = "", maximum: str = "", status: str = ""):
    return services.list_records(q, parameter, minimum, maximum, status)


@app.get("/api/accuracy")
def api_accuracy():
    return services.evaluate_accuracy()
