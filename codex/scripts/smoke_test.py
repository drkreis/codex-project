# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import bootstrap, evaluate_accuracy, list_records, status_snapshot


def main() -> None:
    result = bootstrap()
    snap = status_snapshot()
    acc = evaluate_accuracy()
    records = list_records()
    print("ingested:", result["ingested"])
    print("standards:", snap["standards_count"])
    print("records:", snap["records_count"])
    print("field_accuracy:", acc["field_accuracy"])
    print("sample_record:", records[0]["document_id"] if records else None)
    assert snap["standards_count"] >= 10, "need >=10 standards"
    assert snap["records_count"] >= 10, "need >=10 records"
    assert acc["field_accuracy"] >= 0.95, f"accuracy too low: {acc['field_accuracy']}"
    print("SMOKE OK")


if __name__ == "__main__":
    main()
