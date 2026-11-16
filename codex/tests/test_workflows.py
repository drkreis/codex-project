from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backups, db, services
from app.errors import UserError
from app.parameters import parse_params, quantity


class Workflows(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="codex-test-")))
        self.data = self.temp / "data"
        self.standards = self.data / "standards"
        self.golden = self.data / "golden"
        shutil.copytree(ROOT / "data" / "standards", self.standards)
        shutil.copytree(ROOT / "data" / "golden", self.golden)
        for module, values in (
            (db, {"DATA_DIR": self.data, "DB_PATH": self.data / "test.db", "STANDARDS_DIR": self.standards,
                  "GOLDEN_DIR": self.golden, "UPLOADS_DIR": self.data / "uploads", "BACKUPS_DIR": self.data / "backups"}),
            (services, {"STANDARDS_DIR": self.standards, "GOLDEN_DIR": self.golden}),
        ):
            self.stack.enter_context(patch.multiple(module, **values))
        result = services.bootstrap()
        self.assertEqual(result["ingested"], 10)
        self.assertEqual(result["errors"], [])

    def fingerprint(self):
        return hashlib.sha256(db.DB_PATH.read_bytes()).hexdigest()

    def test_manual_edits_and_deleted_parameter_survive_reimport(self):
        before = services.get_record(1)
        params = dict(before["params"], power_kw="3 кВт")
        params.pop("mass_kg")
        services.update_record(1, {"document_id": "ИСПРАВЛЕННОЕ-ОБОЗНАЧЕНИЕ", "title": "Проверенное название", "params": params})
        path = self.standards / "std_01.txt"
        path.write_text(path.read_text(encoding="utf-8").replace("380 В", "400 В"), encoding="utf-8")
        services.ingest_file(path)
        after = services.get_record(1)
        self.assertEqual(after["document_id"], "ИСПРАВЛЕННОЕ-ОБОЗНАЧЕНИЕ")
        self.assertEqual(after["title"], "Проверенное название")
        self.assertEqual(after["params"]["power_kw"], "3 кВт")
        self.assertNotIn("mass_kg", after["params"])
        self.assertEqual(after["params"]["voltage_v"], "400 В")
        self.assertEqual(len(services.list_records()), 10)
        self.assertEqual(after["standard_id"], before["standard_id"])
        self.assertTrue(after["conflicts"])

    def test_import_is_idempotent_and_does_not_duplicate_history(self):
        services.ingest_directory()
        history = len(services.list_history())
        revisions = [r["revision"] for r in services.list_records()]
        services.ingest_directory()
        self.assertEqual(len(services.list_records()), 10)
        self.assertEqual(len(services.list_history()), history)
        self.assertEqual([r["revision"] for r in services.list_records()], revisions)

    def test_legacy_edits_migrated_to_protected_fields(self):
        legacy = self.temp / "legacy.db"
        schema = db.SCHEMA.replace("    manual_fields_json TEXT NOT NULL DEFAULT '[]',\n", "").replace(
            "    revision INTEGER NOT NULL DEFAULT 0,\n", "").replace("    records_snapshot_json TEXT,\n", "")
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.executescript(schema)
            conn.execute("INSERT INTO records(document_id,product_name) VALUES ('OLD','Old product')")
            conn.execute("INSERT INTO change_log(entity,entity_id,field_name,old_value,new_value) VALUES ('records',1,'title','old','new')")
        with patch.object(db, "DB_PATH", legacy):
            db.init_db()
            row = services.get_record(1)
            self.assertIn("title", row["manual_fields"])
            self.assertEqual(row["revision"], 0)
            db.init_db()  # Migration can safely run on every application start.
            self.assertIn("title", services.get_record(1)["manual_fields"])

    def test_stale_edit_is_rejected_without_writing(self):
        revision = services.get_record(1)["revision"]
        services.update_record(1, {"notes": "First edit", "revision": revision})
        before = self.fingerprint()
        with self.assertRaises(UserError) as error:
            services.update_record(1, {"notes": "Lost update", "revision": revision})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(services.get_record(1)["notes"], "First edit")

    def test_bad_params_do_not_write_record_or_history(self):
        for params in ('{"power_kw":', "power_kw=2 кВт; broken", '{"x":1,"x":2}', '["x"]', {"a": []}):
            with self.subTest(params=params):
                before = self.fingerprint()
                with self.assertRaises(UserError):
                    services.update_record(1, {"params": params})
                self.assertEqual(self.fingerprint(), before)

    def test_create_and_edit_share_the_same_parameter_validation(self):
        record = services.create_record({"document_id": "TEST", "product_name": "Test", "params": "Мощность=2200 Вт; mass_kg=500 г"})
        self.assertEqual(record["params"]["power_kw"], "2200 Вт")
        services.update_record(record["id"], {"params": '{"power_kw": "2,2 кВт", "mass_kg": "0.5 кг"}'})
        self.assertEqual(services.get_record(record["id"])["params"]["power_kw"], "2,2 кВт")
        before = self.fingerprint()
        with self.assertRaises(UserError):
            services.create_record({"document_id": "", "product_name": "Test"})
        self.assertEqual(self.fingerprint(), before)

    def test_search_handles_cyrillic_case(self):
        self.assertEqual(services.list_records("НАСОСНЫЙ")[0]["id"], 1)
        self.assertEqual(services.list_records("насосный")[0]["id"], 1)

    def test_unit_conversion_and_separate_power_dimensions(self):
        self.assertEqual(quantity("2200 Вт", "power_kw"), (2.2, 2.2))
        self.assertEqual(quantity("0,38 кВ", "voltage_v"), (380, 380))
        self.assertIsNone(quantity("25 кВА", "power_kw"))
        matches = services.list_records(parameter="power_kw", minimum="2200 Вт", maximum="3 кВт")
        self.assertEqual({r["id"] for r in matches}, {1, 4})
        self.assertNotIn(6, {r["id"] for r in services.list_records(parameter="power_kw", minimum="1")})
        self.assertEqual({r["id"] for r in services.list_records(parameter="apparent_power_kva", minimum="20")}, {6})

    def test_ranges_are_not_treated_as_single_values(self):
        self.assertEqual(quantity("−20…+40 °C", "temp_c"), (-20, 40))
        self.assertIsNone(quantity("380/220 В", "voltage_v"))
        rows = services.list_records(parameter="temp_c", minimum="-10", maximum="50")
        self.assertEqual({r["id"] for r in rows}, {2, 9})
        with self.assertRaises(UserError):
            services.list_records(parameter="power_kw", minimum="5", maximum="2")
        with self.assertRaises(UserError):
            services.list_records(parameter="", minimum="2")

    def test_unknown_quantity_does_not_match_numeric_filter(self):
        record = services.create_record({"document_id": "NOPOWER", "product_name": "Unknown"})
        self.assertNotIn(record["id"], {r["id"] for r in services.list_records(parameter="power_kw", minimum="0")})

    def test_snapshot_survives_changes_and_deletion(self):
        selection_id = services.save_selection("Насосы", [1, 2], "Для заказчика")
        original = services.get_selection(selection_id)
        original_csv = services.selection_csv(original)
        services.update_record(1, {"product_name": "Changed", "params": {"power_kw": "99 кВт"}})
        services.delete_record(2)
        saved = services.get_selection(selection_id)
        self.assertFalse(saved["legacy"])
        self.assertEqual(saved["records"], original["records"])
        self.assertEqual(services.selection_csv(saved), original_csv)

    def test_selection_rejects_missing_ids_empty_and_too_many(self):
        for ids in ([], [999], list(range(1, 14))):
            with self.subTest(ids=ids), self.assertRaises(UserError):
                services.save_selection("Test", ids)
        self.assertEqual(services.list_selections(), [])

    def test_selection_detects_changed_comparison(self):
        record = services.get_record(1)
        services.update_record(1, {"notes": "Updated"})
        with self.assertRaises(UserError) as error:
            services.save_selection("Test", [1], expected_revisions={"1": record["revision"]})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(services.list_selections(), [])

    def test_csv_has_russian_text_roundtrips_delimiters_and_escapes_formulas(self):
        services.update_record(1, {"notes": '=HYPERLINK("bad")', "product_name": "Насос; модель\nА"})
        saved = services.get_selection(services.save_selection("Выбор; 1", [1], "Первая\nВторая"))
        data = services.selection_csv(saved)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(rows[1]))
        self.assertIn("Насос; модель\nА", rows[1])
        self.assertIn("'=HYPERLINK(\"bad\")", rows[1])

    def test_legacy_selection_is_identified_without_inventing_snapshot(self):
        with db.get_db() as conn:
            conn.execute("INSERT INTO selection_results(query,record_ids) VALUES ('Old','[1,999]')")
        saved = services.get_selection(1)
        self.assertTrue(saved["legacy"])
        self.assertEqual(saved["missing"], 1)
        self.assertEqual(len(saved["records"]), 1)

    def test_invalid_uploads_leave_files_and_db_unchanged(self):
        for name, data in (("empty.txt", b""), ("spaces.txt", b" \n "), ("wrong.exe", b"bad"),
                           ("bad.pdf", b"%PDF-1.7 damaged"), ("binary.txt", b"a\x00b"),
                           ("random.txt", b"not a standard"), ("huge.txt", b"x" * (10 * 1024 * 1024 + 1))):
            with self.subTest(name=name):
                before, files = self.fingerprint(), sorted(p.name for p in self.standards.iterdir())
                with self.assertRaises(UserError):
                    services.import_upload(name, data)
                self.assertEqual(self.fingerprint(), before)
                self.assertEqual(sorted(p.name for p in self.standards.iterdir()), files)

    def test_valid_upload_replaces_same_source_without_duplicate(self):
        path = self.standards / "std_01.txt"
        data = path.read_bytes()
        result = services.import_upload("../std_01.txt", data)
        self.assertEqual(result["record_id"], 1)
        self.assertEqual(len(services.list_records()), 10)
        self.assertEqual(path.read_bytes(), data)

    def test_reparse_keeps_active_document_version_when_old_file_remains(self):
        source = (self.standards / "std_01.txt").read_text(encoding="utf-8")
        services.import_upload("new-version.txt", source.replace("2.2 кВт", "4.8 кВт").encode("utf-8"))
        self.assertEqual(services.get_record(1)["params"]["power_kw"], "4.8 кВт")
        services.ingest_directory()
        self.assertEqual(services.get_record(1)["params"]["power_kw"], "4.8 кВт")
        self.assertEqual(len(services.list_records()), 10)

    def test_invalid_voltage_and_temperature_are_not_saved(self):
        for params in ({"voltage_v": "wrong"}, {"temp_c": "20 F"}, {"mass_kg": "-3 кг"}):
            before = self.fingerprint()
            with self.assertRaises(UserError):
                services.update_record(1, {"params": params})
            self.assertEqual(self.fingerprint(), before)

    def test_new_form_does_not_mix_active_and_apparent_power(self):
        from app.main import payload_from_form
        from starlette.datastructures import FormData
        with self.assertRaises(UserError) as error:
            payload_from_form(FormData({"param_power_kw": "3 кВА"}))
        self.assertEqual(error.exception.field, "param_power_kw")

    def test_reparse_continues_after_bad_document(self):
        (self.standards / "bad.pdf").write_bytes(b"broken")
        results = services.ingest_directory()
        self.assertEqual(sum("error" not in r for r in results), 10)
        self.assertEqual(sum("error" in r for r in results), 1)

    def test_backup_restore_and_automatic_safety_copy(self):
        first = backups.create_backup()
        self.assertEqual(backups.validate_backup(first)["records"], 10)
        services.update_record(1, {"notes": "After backup"})
        safety = backups.restore_backup(first.read_bytes())
        self.assertTrue(safety.is_file())
        self.assertNotEqual(services.get_record(1)["notes"], "After backup")
        with closing(sqlite3.connect(safety)) as conn:
            self.assertEqual(conn.execute("SELECT notes FROM records WHERE id=1").fetchone()[0], "After backup")

    def test_bad_restore_is_rejected_before_current_database_changes(self):
        before = self.fingerprint()
        with self.assertRaises(UserError):
            backups.restore_backup(b"invalid")
        self.assertEqual(self.fingerprint(), before)
        malicious = self.temp / "other.db"
        with closing(sqlite3.connect(malicious)) as conn, conn:
            conn.execute("CREATE TABLE unrelated(x)")
        with self.assertRaises(UserError):
            backups.restore_backup(malicious.read_bytes())
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(backups.list_backups(), [])

    def test_history_contains_manual_edits_imports_and_deleted_data(self):
        services.update_record(1, {"notes": "Проверено", "params": {"power_kw": "2 кВт"}})
        services.delete_record(1)
        history = services.list_history(1)
        self.assertEqual(history[0]["field_name"], "delete")
        self.assertEqual(json.loads(history[0]["new_value"])["notes"], "Проверено")
        self.assertIn("params.power_kw", {h["field_name"] for h in history})
        self.assertIn("notes", {h["field_name"] for h in history})

    def test_all_pages_render_and_keep_theme_control(self):
        from app import main
        from starlette.requests import Request
        selection_id = services.save_selection("Test", [1, 2])
        pages = (
            ("/", main.dashboard, ()), ("/records", main.records_list, ()),
            ("/records/new", main.record_new_form, ()), ("/records/1", main.record_view, (1,)),
            ("/records/1/edit", main.record_edit_form, (1,)), ("/standards", main.standards_page, ()),
            ("/accuracy", main.accuracy_page, ()), ("/selections", main.selections_page, ()),
            ("/selections/1", main.selection_view, (selection_id,)), ("/history", main.history_page, ()),
            ("/backups", main.backups_page, ()),
        )
        for path, view, args in pages:
            with self.subTest(path=path):
                request = Request({"type": "http", "method": "GET", "scheme": "http", "path": path,
                                   "query_string": b"", "headers": [(b"host", b"localhost")],
                                   "server": ("localhost", 8787), "root_path": ""})
                response = view(request, *args)
                self.assertEqual(response.status_code, 200)
                html = response.body.decode("utf-8")
                self.assertIn('id="theme-toggle"', html)
                self.assertNotIn("MVP", html)
                self.assertNotIn("local · SQLite", html)


if __name__ == "__main__":
    unittest.main()
