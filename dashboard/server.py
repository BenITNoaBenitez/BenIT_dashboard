from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "db.sqlite"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def read_logs(limit: int = 50) -> list[dict[str, str | int]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, level, message, created_at
            FROM logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_log(level: str, message: str) -> dict[str, str | int]:
    created_at = datetime.now(timezone.utc).isoformat()
    clean_level = (level or "info").strip().lower()[:32]
    clean_message = (message or "").strip()

    if not clean_message:
        raise ValueError("message is required")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO logs (level, message, created_at)
            VALUES (?, ?, ?)
            """,
            (clean_level, clean_message, created_at),
        )
        conn.commit()
        log_id = int(cursor.lastrowid)

    return {
        "id": log_id,
        "level": clean_level,
        "message": clean_message,
        "created_at": created_at,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/health":
            self.send_json({"status": "ok"})
            return

        if path == "/api/logs":
            self.send_json({"logs": read_logs()})
            return

        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path != "/api/logs":
            self.send_json({"error": "not found"}, status=404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            log = add_log(
                level=str(payload.get("level", "info")),
                message=str(payload.get("message", "")),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status=400)
            return

        self.send_json({"log": log}, status=201)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Hermes dashboard running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
