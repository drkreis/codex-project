"""Exercise HTTP forms and downloads against a temporary local server and DB."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    with tempfile.TemporaryDirectory(prefix="codex-http-test-") as temp:
        data = Path(temp) / "data"
        data.mkdir()
        for name in ("standards", "golden"):
            shutil.copytree(ROOT / "data" / name, data / name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        opener = build_opener(NoRedirect)

        def call(path, fields=None, file=None):
            headers = {}
            body = None
            if file:
                boundary = uuid4().hex
                parts = []
                for key, value in fields or []:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
                filename, content = file
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
                parts += [content, f"\r\n--{boundary}--\r\n".encode()]
                body = b"".join(parts)
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            elif fields is not None:
                body = urlencode(fields).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            try:
                response = opener.open(Request(base + path, body, headers), timeout=10)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read(), response.headers

        env = {**os.environ, "CODEX_DATA_DIR": str(data), "PYTHONIOENCODING": "utf-8"}
        with open(Path(temp) / "server.log", "w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Test server stopped")
                    try:
                        if call("/api/status")[0] == 200:
                            break
                    except (URLError, TimeoutError, ConnectionError):
                        time.sleep(.1)
                else:
                    raise RuntimeError("Test server did not start")

                for route in ("/", "/records", "/records/new", "/records/1", "/records/1/edit",
                              "/standards", "/accuracy", "/selections", "/history", "/backups"):
                    code, body, _ = call(route)
                    assert code == 200, (route, code)
                    assert b'id="theme-toggle"' in body and b"MVP" not in body, route
                print("HTTP pages: OK")

                code, body, _ = call("/api/records?parameter=power_kw&minimum=2200%20%D0%92%D1%82&maximum=3")
                assert code == 200 and {r["id"] for r in json.loads(body)} == {1, 4}
                assert call("/records?parameter=power_kw&minimum=5&maximum=2")[0] == 422
                print("HTTP numeric filters: OK")

                fields = [("document_id", "HTTP-TEST"), ("product_name", "Проверка HTTP"), ("status", "manual"),
                          ("param_power_kw", "ошибка"), ("notes", "Ввод не должен теряться")]
                code, body, _ = call("/records/new", fields)
                assert code == 422 and "Ввод не должен теряться".encode() in body
                assert json.loads(call("/api/status")[1])["records_count"] == 10
                fields[3] = ("param_power_kw", "2200 Вт")
                code, _, headers = call("/records/new", fields)
                assert code == 303
                new_id = int(re.search(r"/records/(\d+)", headers["Location"])[1])
                print("HTTP record validation and creation: OK")

                code, preview, _ = call("/selections/preview", [("record_ids", 1), ("record_ids", new_id)])
                assert code == 200
                import html
                revisions = html.unescape(re.search(r'name="revisions" value="([^"]+)"', preview.decode())[1])
                code, _, headers = call("/selections", [("record_ids", 1), ("record_ids", new_id),
                    ("revisions", revisions), ("query", "Проверка подбора"), ("note", "Заказчик; тест")])
                assert code == 303
                selection_id = int(re.search(r"/selections/(\d+)", headers["Location"])[1])
                csv_path = f"/selections/{selection_id}/export.csv"
                code, exported, headers = call(csv_path)
                assert code == 200 and exported.startswith(b"\xef\xbb\xbf")
                assert "attachment" in headers["Content-Disposition"]
                assert len(list(csv.reader(io.StringIO(exported.decode("utf-8-sig")), delimiter=";"))) == 3
                assert call(f"/records/{new_id}/delete", [])[0] == 303
                assert call(csv_path)[1] == exported
                print("HTTP comparison, snapshot, deletion and CSV: OK")

                record = json.loads(call("/api/records?q=NA-220")[1])[0]
                edit = [("document_id", record["document_id"]), ("product_name", record["product_name"]),
                        ("title", record["title"]), ("status", "verified"), ("revision", record["revision"]),
                        ("notes", "Защищено после HTTP-правки"), ("param_power_kw", "2,5 кВт")]
                assert call("/records/1/edit", edit)[0] == 303
                assert call("/records/1/edit", edit)[0] == 409
                assert call("/standards/reparse", [])[0] == 200
                after = json.loads(call("/api/records?q=NA-220")[1])[0]
                assert after["params"]["power_kw"] == "2,5 кВт"
                assert after["notes"] == "Защищено после HTTP-правки"
                assert "Защищено после HTTP-правки".encode() in call("/history")[1]
                print("HTTP protected edits, stale form and history: OK")

                for filename, content in (("empty.txt", b""), ("wrong.exe", b"bad"), ("broken.pdf", b"%PDF-1.7 broken")):
                    code, body, _ = call("/standards/upload", file=(filename, content))
                    assert code == 422 and b'role="alert"' in body, (filename, code)
                valid = (data / "standards" / "std_01.txt").read_bytes()
                assert call("/standards/upload", file=("std_01.txt", valid))[0] == 303
                assert json.loads(call("/api/status")[1])["records_count"] == 10
                print("HTTP upload errors and valid replacement: OK")

                assert call("/backups", [])[0] == 303
                backup = next((data / "backups").glob("codex-*.db"))
                code, backup_bytes, headers = call("/backups/" + backup.name)
                assert code == 200 and backup_bytes.startswith(b"SQLite format 3\x00")
                assert call("/backups/restore", file=("backup.db", backup_bytes))[0] == 422
                assert call("/backups/restore", [("confirm", "yes")], ("wrong.db", b"invalid"))[0] == 422
                assert call("/backups/restore", [("confirm", "yes")], ("backup.db", backup_bytes))[0] == 303
                assert any("before-restore" in p.name for p in (data / "backups").glob("*.db"))
                assert json.loads(call("/api/status")[1])["records_count"] == 10
                print("HTTP backup download, validation and restore: OK")
                print("INTEGRATION OK")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-10000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
