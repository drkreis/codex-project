from __future__ import annotations

import json
import re
from typing import Any


def normalize_params(params: dict[str, str] | str | None) -> dict[str, str]:
    if params is None:
        return {}
    if isinstance(params, str):
        result: dict[str, str] = {}
        for part in params.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, val = part.split("=", 1)
            result[key.strip()] = val.strip()
        return result
    return {str(k): str(v) for k, v in params.items()}


def _norm_value(value: str) -> str:
    value = value.strip().lower()
    value = value.replace(",", ".")
    value = re.sub(r"\s+", " ", value)
    return value


def fields_to_record(fields: dict[str, Any]) -> dict[str, Any]:
    params = normalize_params(fields.get("params_dict") or fields.get("params"))
    return {
        "document_id": (fields.get("document_id") or "").strip(),
        "title": (fields.get("title") or "").strip(),
        "product_name": (fields.get("product_name") or "").strip(),
        "params_json": json.dumps(params, ensure_ascii=False),
        "constraints_text": (fields.get("constraints") or "").strip(),
        "notes": (fields.get("notes") or "").strip(),
        "status": "parsed",
    }


def values_equal(a: str, b: str) -> bool:
    return _norm_value(a) == _norm_value(b)
