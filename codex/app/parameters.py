from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.errors import UserError
from app.parser.extract import PARAM_ALIASES


@dataclass(frozen=True)
class Parameter:
    key: str
    label: str
    example: str
    unit: str = ""


PARAMETERS = (
    Parameter("power_kw", "Активная мощность", "2,2 кВт", "кВт"),
    Parameter("apparent_power_kva", "Полная мощность", "25 кВА", "кВА"),
    Parameter("voltage_v", "Напряжение", "380 В", "В"),
    Parameter("mass_kg", "Масса", "48 кг", "кг"),
    Parameter("dimensions_mm", "Габариты", "620 × 340 × 410 мм"),
    Parameter("temp_c", "Рабочая температура", "−20…+40 °C", "°C"),
    Parameter("throughput", "Производительность", "12 м³/ч"),
    Parameter("service_life", "Срок службы", "10 лет", "лет"),
)
SPECS = {p.key: p for p in PARAMETERS}
FIELD_LABELS = {
    "document_id": "Обозначение документа", "title": "Название документа",
    "product_name": "Изделие", "constraints_text": "Условия применения",
    "constraints": "Условия применения", "notes": "Примечание",
    "status": "Статус", "params_json": "Характеристики", "params": "Характеристики",
    "create": "Создание записи", "delete": "Удаление записи", "protection": "Защита правок",
}
STATUS_LABELS = {"parsed": "Из документа", "manual": "Введено вручную", "verified": "Проверено", "review": "Нужна проверка"}

# Convert to Parameter.unit. Active and apparent power are separate dimensions.
UNITS = {
    "power_kw": {"": 1, "квт": 1, "kw": 1, "вт": .001, "w": .001, "мвт": 1000, "mw": 1000},
    "apparent_power_kva": {"": 1, "ква": 1, "kva": 1, "ва": .001, "va": .001, "мва": 1000, "mva": 1000},
    "voltage_v": {"": 1, "в": 1, "v": 1, "кв": 1000, "kv": 1000, "мв": .001, "mv": .001},
    "mass_kg": {"": 1, "кг": 1, "kg": 1, "г": .001, "g": .001, "т": 1000, "t": 1000},
    "temp_c": {"": 1, "°c": 1, "°с": 1, "c": 1, "с": 1},
    "service_life": {"": 1, "лет": 1, "год": 1, "года": 1, "г.": 1, "мес": 1 / 12, "мес.": 1 / 12, "месяцев": 1 / 12, "месяц": 1 / 12},
}
NUMBER = r"[+\-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
QUANTITY = re.compile(rf"^\s*({NUMBER})(?:\s*(?:…|\.{{2,3}}|–|—|до|-)\s*({NUMBER}))?\s*([^\d]*)$", re.I)


def label(key: str) -> str:
    if key.startswith("params."):
        return label(key[7:])
    return SPECS[key].label if key in SPECS else FIELD_LABELS.get(key, key)


def canonical_key(key: str) -> str:
    clean = key.strip()
    aliases = {p.label.casefold(): p.key for p in PARAMETERS}
    return aliases.get(clean.casefold(), PARAM_ALIASES.get(clean.casefold(), clean))


def normalize_keys(params: dict[str, str]) -> dict[str, str]:
    result = {}
    for key, value in params.items():
        key = canonical_key(key)
        if key == "power_kw" and re.search(r"(?:[км]?ва|[km]?va)\s*$", str(value), re.I):
            key = "apparent_power_kva"
        if key in result:
            raise UserError(f"Характеристика «{label(key)}» указана дважды.", "params")
        result[key] = str(value).strip()
    return result


def parse_params(value: Any) -> dict[str, str]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                def unique_pairs(pairs):
                    result = {}
                    for k, v in pairs:
                        if k in result:
                            raise UserError(f"Характеристика «{k}» указана дважды.", "params")
                        result[k] = v
                    return result
                value = json.loads(text, object_pairs_hook=unique_pairs)
            except json.JSONDecodeError as exc:
                raise UserError(f"Неверный JSON характеристик: строка {exc.lineno}, столбец {exc.colno}.", "params") from exc
        else:
            value = {}
            for part in re.split(r"[;\n]", text):
                if not part.strip():
                    continue
                if "=" not in part:
                    raise UserError("Для каждой характеристики укажите название и значение.", "params")
                key, val = (s.strip() for s in part.split("=", 1))
                if key in value:
                    raise UserError(f"Характеристика «{key}» указана дважды.", "params")
                value[key] = val
    if not isinstance(value, dict):
        raise UserError("Характеристики должны быть набором названий и значений.", "params")
    if len(value) > 50:
        raise UserError("В одной записи можно указать не более 50 характеристик.", "params")
    result = {}
    for key, val in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise UserError("Укажите название характеристики длиной до 100 символов.", "params")
        if isinstance(val, (dict, list, bool)) or val is None or (isinstance(val, float) and not math.isfinite(val)):
            raise UserError(f"У характеристики «{key}» должно быть текстовое или числовое значение.", "params")
        val = str(val).strip()
        if not val or len(val) > 500:
            raise UserError(f"Укажите значение «{label(key)}» длиной от 1 до 500 символов.", "params")
        result[key.strip()] = val
    return normalize_keys(result)


def read_params(value: str | None) -> tuple[dict[str, str], bool]:
    try:
        return parse_params(value or "{}"), False
    except UserError:
        return {}, True


def quantity(value: str, key: str) -> tuple[float, float] | None:
    clean = str(value).replace("−", "-").replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = QUANTITY.fullmatch(clean)
    if not match or key not in UNITS:
        return None
    unit = re.sub(r"\s+", "", match[3]).casefold()
    factor = UNITS[key].get(unit)
    if factor is None:
        return None
    low = float(match[1].replace(",", ".")) * factor
    high = float((match[2] or match[1]).replace(",", ".")) * factor
    if not math.isfinite(low) or not math.isfinite(high) or low > high:
        return None
    return low, high


def bounds(parameter: str, minimum: str = "", maximum: str = "") -> tuple[float | None, float | None]:
    if not minimum.strip() and not maximum.strip():
        return None, None
    if parameter not in UNITS:
        raise UserError("Выберите характеристику для числового фильтра.", "parameter")
    values = []
    for field, value in (("minimum", minimum), ("maximum", maximum)):
        if not value.strip():
            values.append(None)
            continue
        parsed = quantity(value, parameter)
        if parsed is None or parsed[0] != parsed[1]:
            raise UserError(f"Укажите одно число в поле «{'От' if field == 'minimum' else 'До'}». Можно добавить единицу измерения, например {SPECS[parameter].example}.", field)
        values.append(parsed[0])
    lo, hi = values
    if lo is not None and hi is not None and lo > hi:
        raise UserError("Значение «От» не должно превышать значение «До».", "minimum")
    return lo, hi


def validate_quantity(value: str, key: str) -> None:
    parsed = quantity(value, key)
    if parsed is not None and (key == "temp_c" or parsed[0] >= 0):
        return
    # Multiple rated voltages are valid catalog values, but are deliberately not
    # interpreted as an interval by numeric filtering.
    if key == "voltage_v":
        match = re.fullmatch(rf"\s*({NUMBER}(?:\s*/\s*{NUMBER})+)\s*([^\d]*)", value)
        if match and re.sub(r"\s+", "", match[2]).casefold() in UNITS[key]:
            if all(float(n.strip().replace(",", ".")) >= 0 for n in match[1].split("/")):
                return
    sign = "" if key == "temp_c" else "неотрицательное "
    raise UserError(f"Проверьте «{label(key)}»: укажите {sign}число или диапазон с подходящей единицей, например {SPECS[key].example}.", "param_" + key)
