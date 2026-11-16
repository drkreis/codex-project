from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.errors import UserError

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024

KEY_FIELDS = (
    "document_id",
    "title",
    "product_name",
    "params",
    "constraints",
    "notes",
)

PARAM_ALIASES = {
    "мощность": "power_kw",
    "мощность, квт": "power_kw",
    "power": "power_kw",
    "напряжение": "voltage_v",
    "напряжение, в": "voltage_v",
    "масса": "mass_kg",
    "масса, кг": "mass_kg",
    "габариты": "dimensions_mm",
    "габаритные размеры": "dimensions_mm",
    "температура": "temp_c",
    "рабочая температура": "temp_c",
    "производительность": "throughput",
    "срок службы": "service_life",
}


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise UserError("Документ слишком большой. Максимальный размер — 10 МБ.", "file")
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1251")
            except UnicodeDecodeError as exc:
                raise UserError("Не удалось прочитать текст. Сохраните файл в UTF-8.", "file") from exc
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise UserError("Файл содержит двоичные данные. Загрузите текстовый документ или PDF.", "file")
        return text
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("Для PDF нужен пакет pypdf") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise UserError("PDF защищён паролем. Загрузите незашифрованную копию.", "file")
            if len(reader.pages) > 100:
                raise UserError("В PDF больше 100 страниц. Разделите его на несколько документов.", "file")
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except UserError:
            raise
        except Exception as exc:
            raise UserError("Не удалось прочитать PDF. Возможно, файл повреждён.", "file") from exc
    raise UserError("Этот формат не поддерживается. Выберите TXT, MD или текстовый PDF.", "file")


def _first(patterns: list[str], text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()
    return None


SKIP_PARAM_KEYS = {
    "наименование",
    "название",
    "обозначение",
    "документ",
    "примечание",
    "примечания",
    "ограничения",
    "условия применения",
    "требования",
    "изделие",
    "модель",
    "продукт",
    "наименование изделия",
    "основные характеристики",
    "title",
    "notes",
}


def _extract_params(text: str) -> dict[str, str]:
    params: dict[str, str] = {}
    # строки вида "Мощность: 2.2 кВт" / "Масса — 12 кг"
    for match in re.finditer(
        # только пробелы/табы вокруг разделителя — иначе \s съедает \n и «склеивает» строки
        r"^[ \t\-•*]*(?P<key>[А-Яа-яA-Za-z0-9 ,.\-/]{2,40}?)[ \t]*[:—\-][ \t]*(?P<val>[^\n\r]{1,80})",
        text,
        re.MULTILINE,
    ):
        raw_key = match.group("key").strip().lower()
        val = match.group("val").strip()
        if not val or raw_key in SKIP_PARAM_KEYS:
            continue
        key = PARAM_ALIASES.get(raw_key, re.sub(r"[^a-zа-я0-9]+", "_", raw_key, flags=re.IGNORECASE).strip("_"))
        if key and key not in params:
            params[key] = val
    return params


def extract_fields(text: str, fallback_document_id: str | None = None) -> dict[str, Any]:
    document_id = _first(
        [
            r"(?:Обозначение|Документ|Шифр|ID)\s*[:—\-]\s*([^\n]+)",
            r"\b((?:ТУ|ГОСТ|СТО|ISO)[\s\-]*\d[\d.\-/]*)\b",
        ],
        text,
    ) or fallback_document_id or "UNKNOWN"

    title = _first(
        [
            r"(?:Наименование|Название документа|Title)\s*[:—\-]\s*([^\n]+)",
            r"^#\s*(.+)$",
        ],
        text,
    ) or "Без названия"

    product_name = _first(
        [
            r"(?:Изделие|Модель|Продукт|Наименование изделия)\s*[:—\-]\s*([^\n]+)",
        ],
        text,
    ) or title

    constraints = _first(
        [
            r"(?:Ограничения|Условия применения|Требования)\s*[:—\-]\s*([^\n]+)",
        ],
        text,
    ) or ""

    notes = _first(
        [
            r"(?:Примечание|Примечания|Notes)\s*[:—\-]\s*([^\n]+)",
        ],
        text,
    ) or ""

    params = _extract_params(text)
    # сериализуем params как "k=v; k2=v2" для хранения в extracted_fields
    params_str = "; ".join(f"{k}={v}" for k, v in params.items())

    confidences = {
        "document_id": 0.95 if document_id != "UNKNOWN" else 0.4,
        "title": 0.9 if title != "Без названия" else 0.4,
        "product_name": 0.9 if product_name else 0.4,
        "params": 0.85 if params else 0.3,
        "constraints": 0.8 if constraints else 0.3,
        "notes": 0.75 if notes else 0.3,
    }

    return {
        "document_id": document_id,
        "title": title,
        "product_name": product_name,
        "params": params_str,
        "params_dict": params,
        "constraints": constraints,
        "notes": notes,
        "confidences": confidences,
    }


def parse_document(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if not text.strip():
        message = "В PDF нет извлекаемого текста. Для скана сначала нужно распознавание текста." if path.suffix.lower() == ".pdf" else "Документ пуст. Выберите файл с текстом."
        raise UserError(message, "file")
    if len(text) > 2_000_000:
        raise UserError("В документе слишком много текста. Разделите его на части.", "file")
    fields = extract_fields(text, fallback_document_id=path.stem.upper())
    if fields["title"] == "Без названия" and not fields["params_dict"] and fields["document_id"] == path.stem.upper():
        raise UserError("Не найдены обозначение, название или характеристики. Проверьте содержание документа.", "file")
    fields["source_text"] = text
    fields["format"] = path.suffix.lower().lstrip(".") or "txt"
    fields["file_path"] = str(path)
    return fields
