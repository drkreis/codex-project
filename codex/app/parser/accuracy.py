from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .extract import KEY_FIELDS
from .normalize import normalize_params, values_equal


def _field_equal(key: str, expected: str, got: str) -> bool:
    if key == "params":
        exp_map = normalize_params(expected)
        got_map = normalize_params(got)
        if set(exp_map) != set(got_map):
            return False
        return all(values_equal(exp_map[k], got_map[k]) for k in exp_map)
    return values_equal(expected, got)


def compare_to_golden(predicted: dict[str, Any], golden: dict[str, Any]) -> dict[str, Any]:
    field_results = {}
    hits = 0
    total = 0
    for key in KEY_FIELDS:
        if key not in golden:
            continue
        total += 1
        exp = str(golden.get(key, ""))
        got = str(predicted.get(key, ""))
        ok = _field_equal(key, exp, got)
        if ok:
            hits += 1
        field_results[key] = {"expected": exp, "got": got, "ok": ok}
    accuracy = (hits / total) if total else 0.0
    return {
        "hits": hits,
        "total": total,
        "accuracy": round(accuracy, 4),
        "fields": field_results,
        "document_ok": accuracy == 1.0,
    }


def load_golden(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def score_batch(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """pairs: list of (predicted, golden). Accuracy = доля верных полей по всем документам."""
    per_docs = []
    hits = 0
    total = 0
    for predicted, golden in pairs:
        one = compare_to_golden(predicted, golden)
        per_docs.append(one)
        hits += one["hits"]
        total += one["total"]
    return {
        "documents": len(pairs),
        "field_hits": hits,
        "field_total": total,
        "field_accuracy": round((hits / total) if total else 0.0, 4),
        "documents_perfect": sum(1 for d in per_docs if d["document_ok"]),
        "details": per_docs,
    }
