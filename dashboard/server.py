"""
Agent B — Backend API
pip install fastapi uvicorn python-multipart aiofiles
Run: uvicorn server:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Literal
import sqlite3, json, time, uuid, os
from pathlib import Path

app = FastAPI(title="Agent B API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve frontend ─────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent / "dashboard"
app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(str(DASHBOARD_DIR / "index.html"))

# ── Database ───────────────────────────────────────────────────
DB = Path(__file__).parent / "agent_b.db"

def get_db():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            level       TEXT    DEFAULT 'info',
            agent       TEXT    DEFAULT 'System',
            message     TEXT    NOT NULL,
            metadata    TEXT,
            created_at  REAL    DEFAULT (unixepoch('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            type        TEXT    DEFAULT 'li',
            status      TEXT    DEFAULT 'todo',
            agent       TEXT,
            metadata    TEXT,
            created_at  REAL    DEFAULT (unixepoch('now')),
            updated_at  REAL    DEFAULT (unixepoch('now'))
        );
        CREATE TABLE IF NOT EXISTS roi_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT,
            value_eur   REAL    DEFAULT 0,
            hours       REAL    DEFAULT 0,
            agent       TEXT    DEFAULT 'System',
            created_at  REAL    DEFAULT (unixepoch('now'))
        );
        CREATE TABLE IF NOT EXISTS scheduled_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT    NOT NULL,
            agent        TEXT,
            scheduled_at REAL,
            status       TEXT    DEFAULT 'pending',
            metadata     TEXT,
            created_at   REAL    DEFAULT (unixepoch('now'))
        );
        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            value       REAL    NOT NULL,
            recorded_at REAL    DEFAULT (unixepoch('now'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT,
            type        TEXT    DEFAULT 'text',
            file_path   TEXT,
            direction   TEXT    DEFAULT 'to_agent',
            created_at  REAL    DEFAULT (unixepoch('now'))
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ══════════════════════════════════════════════════════════════
#  HEALTH  (used by Hermes / monitoring to confirm the server is up)
# ══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    """Liveness check — confirms the API is up and the database is reachable."""
    try:
        conn = get_db()
        logs = conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('pending_validation','waiting')"
        ).fetchone()["n"]
        conn.close()
        return {"status": "ok", "time": time.time(), "logs": logs, "pending_validations": pending}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})

# ══════════════════════════════════════════════════════════════
#  LOGS
# ══════════════════════════════════════════════════════════════

class LogCreate(BaseModel):
    level:   Literal['info', 'warn', 'warning', 'error', 'err', 'ok'] = 'info'
    agent:   str = 'System'
    message: str
    metadata: Optional[dict] = None

@app.get("/api/logs")
async def get_logs(limit: int = 100, agent: Optional[str] = None, level: Optional[str] = None):
    conn = get_db()
    query = "SELECT * FROM logs"
    params = []
    conditions = []
    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    if level:
        conditions.append("level = ?")
        params.append(level)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"logs": [dict(r) for r in rows]}

@app.post("/api/logs", status_code=201)
async def create_log(log: LogCreate):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO logs (level, agent, message, metadata) VALUES (?,?,?,?)",
        (log.level, log.agent, log.message, json.dumps(log.metadata) if log.metadata else None)
    )
    conn.commit()
    log_id = c.lastrowid
    conn.close()
    return {"id": log_id, "status": "created"}

# ══════════════════════════════════════════════════════════════
#  TASKS
# ══════════════════════════════════════════════════════════════

class TaskCreate(BaseModel):
    title:    str
    type:     str = 'li'
    status:   str = 'todo'
    agent:    Optional[str] = None
    metadata: Optional[dict] = None

class TaskUpdate(BaseModel):
    status: Optional[str] = None
    title:  Optional[str] = None

@app.get("/api/tasks")
async def get_tasks(status: Optional[str] = None):
    conn = get_db()
    if status:
        rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"tasks": [dict(r) for r in rows]}

@app.post("/api/tasks", status_code=201)
async def create_task(task: TaskCreate):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title, type, status, agent, metadata) VALUES (?,?,?,?,?)",
        (task.title, task.type, task.status, task.agent, json.dumps(task.metadata) if task.metadata else None)
    )
    conn.commit()
    task_id = c.lastrowid
    conn.close()
    return {"task": {"id": task_id, "title": task.title, "status": task.status}}

@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, update: TaskUpdate):
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status=COALESCE(?,status), title=COALESCE(?,title), updated_at=unixepoch('now') WHERE id=?",
        (update.status, update.title, task_id)
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

# ══════════════════════════════════════════════════════════════
#  ROI
# ══════════════════════════════════════════════════════════════

class ROIEntry(BaseModel):
    label:     str
    value_eur: float
    hours:     float = 0
    agent:     str = 'System'

@app.get("/api/roi")
async def get_roi():
    conn = get_db()
    entries = conn.execute("SELECT * FROM roi_entries ORDER BY created_at DESC").fetchall()
    conn.close()
    entries = [dict(e) for e in entries]
    return {
        "total_value_eur": sum(e["value_eur"] for e in entries),
        "total_hours":     sum(e["hours"]     for e in entries),
        "entries":         entries,
    }

@app.post("/api/roi", status_code=201)
async def add_roi(entry: ROIEntry):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO roi_entries (label, value_eur, hours, agent) VALUES (?,?,?,?)",
        (entry.label, entry.value_eur, entry.hours, entry.agent)
    )
    conn.commit()
    roi_id = c.lastrowid
    conn.close()
    return {"id": roi_id, "status": "created"}

# ══════════════════════════════════════════════════════════════
#  SCHEDULED ACTIONS
# ══════════════════════════════════════════════════════════════

class ScheduledCreate(BaseModel):
    title:        str
    agent:        str
    scheduled_at: float          # unix timestamp (seconds)
    metadata:     Optional[dict] = None

class ScheduledUpdate(BaseModel):
    status: Optional[str] = None

@app.get("/api/scheduled")
async def get_scheduled(status: str = 'pending'):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM scheduled_actions WHERE status=? ORDER BY scheduled_at ASC",
        (status,)
    ).fetchall()
    conn.close()
    return {"scheduled": [dict(r) for r in rows]}

@app.post("/api/scheduled", status_code=201)
async def create_scheduled(action: ScheduledCreate):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO scheduled_actions (title, agent, scheduled_at, metadata) VALUES (?,?,?,?)",
        (action.title, action.agent, action.scheduled_at, json.dumps(action.metadata) if action.metadata else None)
    )
    conn.commit()
    action_id = c.lastrowid
    conn.close()
    return {"id": action_id, "status": "created"}

@app.put("/api/scheduled/{action_id}")
async def update_scheduled(action_id: int, update: ScheduledUpdate):
    conn = get_db()
    conn.execute(
        "UPDATE scheduled_actions SET status=COALESCE(?,status) WHERE id=?",
        (update.status, action_id)
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}

# ══════════════════════════════════════════════════════════════
#  METRICS  (per-agent time-series)
# ══════════════════════════════════════════════════════════════

class MetricPost(BaseModel):
    agent: str
    key:   str
    value: float

@app.get("/api/metrics/{agent}")
async def get_metrics(agent: str, days: int = 30):
    conn = get_db()
    since = time.time() - days * 86400
    rows = conn.execute(
        "SELECT key, value, recorded_at FROM metrics WHERE agent=? AND recorded_at>? ORDER BY recorded_at DESC",
        (agent, since)
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    # Latest value per key
    latest = {}
    for r in rows:
        if r["key"] not in latest:
            latest[r["key"]] = r["value"]
    return {"agent": agent, "latest": latest, "history": rows}

@app.post("/api/metrics", status_code=201)
async def post_metric(m: MetricPost):
    conn = get_db()
    conn.execute("INSERT INTO metrics (agent, key, value) VALUES (?,?,?)", (m.agent, m.key, m.value))
    conn.commit()
    conn.close()
    return {"status": "recorded"}

# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

class TelegramMsg(BaseModel):
    message: str
    task_id: Optional[int] = None

@app.post("/api/telegram/send")
async def telegram_send(msg: TelegramMsg):
    # Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        import urllib.request, urllib.parse
        text = msg.message
        if msg.task_id:
            text = f"[Tâche #{msg.task_id}] {text}"
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    return {"status": "sent"}

# ══════════════════════════════════════════════════════════════
#  AGENT COMMUNICATION
# ══════════════════════════════════════════════════════════════

class AgentMessage(BaseModel):
    content: str
    ts:      Optional[str] = None

@app.post("/api/agent/message", status_code=201)
async def agent_message(data: AgentMessage):
    """Text message or note from the dashboard user to the agent."""
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (content, type, direction) VALUES (?,?,?)",
        (data.content, 'text', 'to_agent')
    )
    # Also log it so it shows up in the activity feed
    conn.execute(
        "INSERT INTO logs (level, agent, message) VALUES (?,?,?)",
        ('info', 'Dashboard', f'Message utilisateur : "{data.content[:120]}"')
    )
    conn.commit()
    conn.close()
    return {"status": "received"}

@app.post("/api/agent/audio", status_code=201)
async def agent_audio(audio: UploadFile = File(...)):
    """Voice message from the dashboard user."""
    audio_dir = Path(__file__).parent / "uploads" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4()}.webm"
    path = audio_dir / filename
    with open(path, "wb") as f:
        f.write(await audio.read())
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (content, type, file_path, direction) VALUES (?,?,?,?)",
        ('Message vocal', 'audio', str(path), 'to_agent')
    )
    conn.execute(
        "INSERT INTO logs (level, agent, message) VALUES (?,?,?)",
        ('info', 'Dashboard', 'Message vocal reçu')
    )
    conn.commit()
    conn.close()
    return {"status": "received", "file": filename}

@app.post("/api/agent/document", status_code=201)
async def agent_document(doc: UploadFile = File(...)):
    """Document upload from the dashboard user."""
    doc_dir = Path(__file__).parent / "uploads" / "documents"
    doc_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4()}_{doc.filename}"
    path = doc_dir / filename
    with open(path, "wb") as f:
        f.write(await doc.read())
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (content, type, file_path, direction) VALUES (?,?,?,?)",
        (doc.filename, 'document', str(path), 'to_agent')
    )
    conn.execute(
        "INSERT INTO logs (level, agent, message) VALUES (?,?,?)",
        ('info', 'Dashboard', f'Document reçu : {doc.filename}')
    )
    conn.commit()
    conn.close()
    return {"status": "received", "file": filename}

@app.get("/api/agent/messages")
async def get_messages(direction: str = 'to_agent', limit: int = 50):
    """Agent reads messages sent from the dashboard."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE direction=? ORDER BY created_at DESC LIMIT ?",
        (direction, limit)
    ).fetchall()
    conn.close()
    return {"messages": [dict(r) for r in rows]}

# ══════════════════════════════════════════════════════════════
#  AUTOMATION REQUESTS  ("cette tâche est-elle automatisable ?")
# ══════════════════════════════════════════════════════════════

AUTOMATION_EMAIL = os.getenv("AUTOMATION_EMAIL", "benitez.noapro@gmail.com")

def _send_email(subject: str, body: str, to_addr: str) -> bool:
    """Send an email if SMTP_* env vars are set. Returns True on success, False otherwise.
    Configure with: SMTP_HOST, SMTP_USER, SMTP_PASS, optional SMTP_PORT (587/465), SMTP_FROM."""
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    pwd  = os.getenv("SMTP_PASS")
    if not (host and user and pwd):
        return False
    import smtplib, ssl
    from email.message import EmailMessage
    port   = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM", user)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_addr
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
                s.login(user, pwd); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                s.starttls(context=ssl.create_default_context()); s.login(user, pwd); s.send_message(msg)
        return True
    except Exception:
        return False

class AutomationRequest(BaseModel):
    task:    str
    contact: Optional[str] = None

@app.post("/api/automation/request", status_code=201)
async def automation_request(req: AutomationRequest):
    """A dashboard user asks whether a task can be automated.
    Stored as a message the agent can read, logged to the activity feed, and emailed."""
    body = req.task.strip()
    if req.contact:
        body += f"\n\nContact : {req.contact}"
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (content, type, direction) VALUES (?,?,?)",
        (body, 'automation', 'to_agent')
    )
    conn.execute(
        "INSERT INTO logs (level, agent, message) VALUES (?,?,?)",
        ('info', 'Dashboard', f"Demande d'automatisation : \"{req.task[:120]}\"")
    )
    conn.commit()
    conn.close()
    emailed = _send_email(
        "Nouvelle demande d'automatisation — Dashboard",
        body,
        AUTOMATION_EMAIL,
    )
    return {"status": "received", "emailed": emailed, "to": AUTOMATION_EMAIL}

# ══════════════════════════════════════════════════════════════
#  AGENT INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════
#
#  Your agent should call these endpoints to push data to the dashboard.
#
#  QUICK REFERENCE — what to call and when:
#
#  Check the server is alive (and DB reachable):
#    GET /api/health   ->  { "status": "ok", "time": ..., "logs": N, "pending_validations": N }
#
#  Every action taken:
#    POST /api/logs
#    { "level": "info", "agent": "LinkedIn", "message": "Connexion envoyée · Sophie Lambert" }
#
#  When an action needs user approval:
#    POST /api/tasks
#    { "title": "Publier ce post LinkedIn : …", "type": "li", "status": "pending_validation", "agent": "LinkedIn" }
#
#  When a task is completed:
#    PUT /api/tasks/{id}   { "status": "done" }
#
#  When a future action is scheduled:
#    POST /api/scheduled
#    { "title": "Envoi campagne email", "agent": "Email", "scheduled_at": 1718000000.0 }
#
#  When a scheduled action executes:
#    PUT /api/scheduled/{id}   { "status": "done" }
#
#  To record ROI (time saved, revenue generated):
#    POST /api/roi
#    { "label": "Réponse email automatique", "value_eur": 50, "hours": 0.5, "agent": "Email" }
#
#  To record a metric data point (for charts):
#    POST /api/metrics
#    { "agent": "LinkedIn", "key": "posts_published", "value": 1 }
#
#  To read messages/notes the user sent from the dashboard:
#    GET /api/agent/messages?direction=to_agent
#    (note: "cette tâche est-elle automatisable ?" requests arrive here with type="automation")
#
#  Automation requests from the "Demander si une tâche est automatisable" button:
#    POST /api/automation/request  { "task": "...", "contact": "..." }
#    -> stored as an agent message (type=automation), logged, and emailed to AUTOMATION_EMAIL.
#    Configure outbound email with env vars: SMTP_HOST, SMTP_USER, SMTP_PASS,
#    optional SMTP_PORT (587 STARTTLS / 465 SSL) and SMTP_FROM. Recipient defaults to
#    benitez.noapro@gmail.com (override with AUTOMATION_EMAIL).
#
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
