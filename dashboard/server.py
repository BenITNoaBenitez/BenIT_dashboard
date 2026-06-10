from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT_DIR / "dashboard"

# Bases de données
AGENT_DB = Path("/opt/data/kanban.db")        # tâches réelles d'Hermes
STATE_DB = Path("/opt/data/state.db")          # messages/logs réels d'Hermes
DASH_DB = ROOT_DIR / "agent_b.db"             # données propres au dashboard (ROI, pending, logs dashboard)

SKILLS_DIR = Path("/opt/data/skills")
CRON_FILE = Path("/opt/data/cron/jobs.json")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


# ── ENV ────────────────────────────────────────────────────────────────────────
def _env(key: str) -> str:
    val = os.environ.get(key, "")
    if val:
        return val
    env_file = Path("/opt/data/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ── DB INIT ────────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DASH_DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL DEFAULT 'info',
            agent TEXT DEFAULT 'Système',
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS roi_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL,
            duration_minutes REAL NOT NULL,
            hours_saved REAL NOT NULL,
            value_eur REAL NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            action_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            meta TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""")
        try:
            conn.execute("ALTER TABLE logs ADD COLUMN agent TEXT DEFAULT 'Système'")
        except Exception:
            pass
        conn.commit()


# ── LOGS — lit depuis state.db (vrais messages Hermes) + dash_db ──────────────
def read_logs(limit: int = 100, agent: str = None) -> list[dict]:
    results = []

    # 1. Logs dashboard (POST /api/logs)
    try:
        with sqlite3.connect(DASH_DB) as conn:
            conn.row_factory = sqlite3.Row
            if agent:
                rows = conn.execute(
                    "SELECT id, level, agent, message, created_at FROM logs WHERE agent=? ORDER BY id DESC LIMIT ?",
                    (agent, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, level, agent, message, created_at FROM logs ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            results.extend([dict(r) for r in rows])
    except Exception:
        pass

    # 2. Messages Hermes depuis state.db (si pas de filtre agent)
    if not agent and STATE_DB.exists():
        try:
            with sqlite3.connect(STATE_DB) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT id, role, content, timestamp FROM messages
                    WHERE role IN ('assistant','tool')
                    ORDER BY id DESC LIMIT ?""",
                    (limit,)
                ).fetchall()
                for r in rows:
                    content = str(r["content"] or "")
                    # Filtrer les messages trop courts ou JSON pur
                    if len(content) < 10:
                        continue
                    # Tronquer les messages longs
                    msg = content[:200].replace('\n', ' ')
                    ts = r["timestamp"]
                    if ts and isinstance(ts, (int, float)):
                        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    results.append({
                        "id": f"h_{r['id']}",
                        "level": "info",
                        "agent": "Hermes" if r["role"] == "assistant" else "Tool",
                        "message": msg,
                        "created_at": ts or ""
                    })
        except Exception:
            pass

    # Trier par date décroissante
    def sort_key(x):
        ts = x.get("created_at") or ""
        return str(ts)

    results.sort(key=sort_key, reverse=True)
    return results[:limit]


def add_log(level: str, message: str, agent: str = "Système") -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    if not message.strip():
        raise ValueError("message is required")
    with sqlite3.connect(DASH_DB) as conn:
        cur = conn.execute(
            "INSERT INTO logs (level,agent,message,created_at) VALUES (?,?,?,?)",
            (level.strip().lower()[:32], agent.strip()[:64], message.strip(), created_at)
        )
        conn.commit()
    return {"id": cur.lastrowid, "level": level, "agent": agent, "message": message, "created_at": created_at}


# ── TASKS — lit depuis kanban.db (vraies tâches Hermes) ──────────────────────
def read_tasks() -> list[dict]:
    tasks = []

    # 1. Tâches Hermes depuis kanban.db
    if AGENT_DB.exists():
        try:
            with sqlite3.connect(AGENT_DB) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT id, title, status, created_at FROM tasks
                    ORDER BY CASE status
                        WHEN 'doing' THEN 0
                        WHEN 'todo' THEN 1
                        WHEN 'in_progress' THEN 0
                        WHEN 'pending' THEN 1
                        ELSE 2 END, id DESC
                    LIMIT 50"""
                ).fetchall()
                for r in rows:
                    status = r["status"]
                    # Normaliser les statuts
                    if status in ("in_progress", "doing", "running"):
                        status = "doing"
                    elif status in ("done", "completed", "finished"):
                        status = "done"
                    elif status in ("todo", "pending", "queued"):
                        status = "todo"
                    tasks.append({
                        "id": f"k_{r['id']}",
                        "title": r["title"] or "",
                        "type": "li",
                        "status": status,
                        "created_at": r["created_at"] or "",
                        "source": "hermes"
                    })
        except Exception:
            pass

    # 2. Tâches dashboard (ajoutées manuellement)
    try:
        with sqlite3.connect(DASH_DB) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                rows = conn.execute(
                    "SELECT id,title,type,status,created_at FROM tasks ORDER BY id DESC LIMIT 20"
                ).fetchall()
                tasks.extend([{**dict(r), "source": "dashboard"} for r in rows])
    except Exception:
        pass

    return tasks


def add_task(title: str, type_: str = "li", status: str = "todo") -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    # Créer la table si besoin
    with sqlite3.connect(DASH_DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            type TEXT DEFAULT 'li',
            status TEXT DEFAULT 'todo',
            created_at TEXT NOT NULL
        )""")
        cur = conn.execute(
            "INSERT INTO tasks (title,type,status,created_at) VALUES (?,?,?,?)",
            (title.strip(), type_.strip(), status.strip(), created_at)
        )
        conn.commit()
    return {"id": cur.lastrowid, "title": title, "type": type_, "status": status, "created_at": created_at}


def update_task(task_id: str, status: str) -> dict | None:
    # Tâche kanban
    if str(task_id).startswith("k_"):
        real_id = str(task_id)[2:]
        status_map = {"done": "done", "doing": "in_progress", "todo": "pending"}
        db_status = status_map.get(status, status)
        with sqlite3.connect(AGENT_DB) as conn:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (db_status, real_id))
            conn.commit()
        return {"id": task_id, "status": status}
    # Tâche dashboard
    try:
        with sqlite3.connect(DASH_DB) as conn:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def delete_task(task_id) -> bool:
    try:
        with sqlite3.connect(DASH_DB) as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()
    except Exception:
        pass
    return True


# ── ROI ────────────────────────────────────────────────────────────────────────
def read_roi() -> dict:
    with sqlite3.connect(DASH_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM roi_entries ORDER BY id DESC LIMIT 50").fetchall()
    entries = [dict(r) for r in rows]
    return {
        "total_hours": round(sum(e["hours_saved"] for e in entries), 2),
        "total_value_eur": round(sum(e["value_eur"] for e in entries), 2),
        "rate_per_hour": 50,
        "entries": entries
    }


def add_roi(task_name: str, duration_minutes: float) -> dict:
    hours_saved = duration_minutes / 60
    value_eur = hours_saved * 50
    created_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DASH_DB) as conn:
        conn.execute(
            "INSERT INTO roi_entries (task_name,duration_minutes,hours_saved,value_eur,created_at) VALUES (?,?,?,?,?)",
            (task_name, duration_minutes, round(hours_saved, 4), round(value_eur, 2), created_at)
        )
        conn.commit()
    return {"task_name": task_name, "hours_saved": round(hours_saved, 4), "value_eur": round(value_eur, 2)}


# ── PENDING ────────────────────────────────────────────────────────────────────
def read_pending() -> list[dict]:
    with sqlite3.connect(DASH_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pending_actions WHERE status='pending' ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_pending(agent: str, action_type: str, title: str, content: str = "", meta: str = "") -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DASH_DB) as conn:
        cur = conn.execute(
            "INSERT INTO pending_actions (agent,action_type,title,content,meta,status,created_at) VALUES (?,?,?,?,?,'pending',?)",
            (agent, action_type, title, content, meta, created_at)
        )
        conn.commit()
    return {"id": cur.lastrowid, "agent": agent, "action_type": action_type, "title": title}


def resolve_pending(action_id: int, approved: bool) -> dict | None:
    status = "approved" if approved else "rejected"
    with sqlite3.connect(DASH_DB) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, action_id))
        conn.commit()
        row = conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
    return dict(row) if row else None


# ── SCHEDULED / CRONS ─────────────────────────────────────────────────────────
def read_scheduled() -> list[dict]:
    if not CRON_FILE.exists():
        return []
    try:
        data = json.loads(CRON_FILE.read_text())
        result = []
        for j in data.get("jobs", []):
            result.append({
                "id": j.get("id", ""),
                "title": (j.get("name") or "")[:50],
                "agent": _guess_agent(j.get("name", "")),
                "schedule": j.get("schedule_display", ""),
                "status": "pending" if j.get("enabled", True) else "paused",
                "state": j.get("state", "scheduled"),
                "last_run": (j.get("last_run_at") or "")[:16],
                "last_status": j.get("last_status", ""),
                "enabled": j.get("enabled", True),
                "next_run": (j.get("next_run_at") or "")[:16],
            })
        return result
    except Exception:
        return []


def _guess_agent(name: str) -> str:
    n = name.lower()
    if "linkedin" in n or "sourcing" in n:
        return "LinkedIn"
    if "email" in n or "mail" in n:
        return "Email"
    if "post" in n or "publish" in n:
        return "CONTENU"
    return "Système"


# ── SKILLS ─────────────────────────────────────────────────────────────────────
def scan_skills() -> dict:
    nodes, links = [], []
    if not SKILLS_DIR.exists():
        return {"nodes": nodes, "links": links}
    for f in list(SKILLS_DIR.rglob("SKILL.md"))[:60]:
        try:
            rel = f.relative_to(SKILLS_DIR)
            parts = rel.parts
            node_id = str(rel.parent)
            label = parts[-2] if len(parts) > 1 else parts[0]
            nodes.append({"id": node_id, "label": label[:22], "group": parts[0]})
            content = f.read_text(errors="ignore")
            for m in re.finditer(r'\[\[([^\]]+)\]\]', content):
                target = m.group(1).strip()
                for n in nodes:
                    if target.lower() in n["label"].lower() and n["id"] != node_id:
                        links.append({"s": node_id, "t": n["id"]})
        except Exception:
            pass
    return {"nodes": nodes, "links": links}


def read_skill_content(skill_id: str) -> dict:
    safe = skill_id.replace("..", "").strip("/")
    for p in [SKILLS_DIR / safe / "SKILL.md", SKILLS_DIR / safe]:
        if p.exists() and p.is_file():
            return {"id": skill_id, "label": Path(safe).name, "content": p.read_text(errors="ignore")}
    return {"id": skill_id, "label": skill_id, "content": ""}


# ── TELEGRAM ───────────────────────────────────────────────────────────────────
def send_to_queue(message: str, task_id=None) -> None:
    try:
        with open("/opt/data/dashboard_queue.txt", "a") as f:
            f.write(message + "\n")
    except Exception:
        pass


def send_via_telethon(message: str) -> bool:
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        api_id = int(_env("TELEGRAM_API_ID") or 0)
        api_hash = _env("TELEGRAM_API_HASH")
        session = _env("TELEGRAM_SESSION_STRING")
        bot = _env("TELEGRAM_BOT_USERNAME")
        if not all([api_id, api_hash, session, bot]):
            raise ValueError("missing config")
        with TelegramClient(StringSession(session), api_id, api_hash) as client:
            client.send_message(bot, message)
        return True
    except Exception:
        token = _env("TELEGRAM_BOT_TOKEN")
        chat_id = _env("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return False
        try:
            payload = json.dumps({"chat_id": chat_id, "text": f"[Dashboard] {message}"}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200
        except Exception:
            return False


# ── HANDLER ────────────────────────────────────────────────────────────────────
class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if p == "/api/health":
            self.send_json({"status": "ok", "time": datetime.now(timezone.utc).timestamp()})
        elif p == "/api/logs":
            agent = qs.get("agent", [None])[0]
            limit = int(qs.get("limit", ["100"])[0])
            self.send_json({"logs": read_logs(limit=limit, agent=agent)})
        elif p == "/api/tasks":
            self.send_json({"tasks": read_tasks()})
        elif p == "/api/roi":
            self.send_json(read_roi())
        elif p == "/api/pending":
            self.send_json({"pending": read_pending()})
        elif p in ("/api/scheduled", "/api/crons"):
            self.send_json({"scheduled": read_scheduled(), "crons": read_scheduled()})
        elif p == "/api/skills":
            self.send_json(scan_skills())
        elif p.startswith("/api/skills/"):
            self.send_json(read_skill_content(p[len("/api/skills/"):]))
        else:
            super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            payload = self._body()
        except Exception as e:
            self.send_json({"error": str(e)}, 400)
            return

        if p == "/api/logs":
            try:
                log = add_log(
                    level=str(payload.get("level", "info")),
                    message=str(payload.get("message", "")),
                    agent=str(payload.get("agent", "Système"))
                )
                self.send_json({"log": log}, 201)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)

        elif p == "/api/tasks":
            task = add_task(
                title=str(payload.get("title", "")),
                type_=str(payload.get("type", "li")),
                status=str(payload.get("status", "todo"))
            )
            self.send_json({"task": task}, 201)

        elif p == "/api/roi":
            entry = add_roi(
                task_name=str(payload.get("task_name", "tâche")),
                duration_minutes=float(payload.get("duration_minutes", 0))
            )
            self.send_json(entry, 201)

        elif p == "/api/pending":
            action = add_pending(
                agent=str(payload.get("agent", "Agent")),
                action_type=str(payload.get("action_type", "action")),
                title=str(payload.get("title", "")),
                content=str(payload.get("content", "")),
                meta=str(payload.get("meta", ""))
            )
            self.send_json({"action": action}, 201)

        elif p in ("/api/telegram/send", "/api/agent/message"):
            message = str(payload.get("message", ""))
            task_id = payload.get("task_id")
            send_to_queue(message, task_id)
            sent = send_via_telethon(message)
            self.send_json({"sent": sent})

        elif p == "/api/agent/audio":
            self.send_json({"received": True})

        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path
        try:
            payload = self._body()
        except Exception as e:
            self.send_json({"error": str(e)}, 400)
            return

        m = re.match(r"^/api/tasks/(.+)$", p)
        if m:
            task = update_task(m.group(1), str(payload.get("status", "todo")))
            self.send_json({"task": task} if task else {"error": "not found"})
            return

        m = re.match(r"^/api/pending/(\d+)$", p)
        if m:
            approved = payload.get("approved", False)
            action = resolve_pending(int(m.group(1)), approved)
            if action and approved:
                send_via_telethon(f"✅ Approuvé : {action.get('title','')}")
            self.send_json({"action": action} if action else {"error": "not found"})
            return

        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        m = re.match(r"^/api/tasks/(.+)$", p)
        if m:
            delete_task(m.group(1))
            self.send_json({"deleted": True})
            return
        self.send_json({"error": "not found"}, 404)


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Agent B dashboard → http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
