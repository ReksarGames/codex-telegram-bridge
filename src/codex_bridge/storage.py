"""Small metadata store. Prompts are kept only until their task becomes terminal."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .security import Rejected

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE = {"dispatching", "running", "waiting_approval", "waiting_user", "waiting_reference", "unknown"}
STATES = ACTIVE | TERMINAL | {"queued"}


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise Rejected("Unsupported database version")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
          thread TEXT UNIQUE,session TEXT,state TEXT NOT NULL DEFAULT 'stopped',paused INTEGER NOT NULL DEFAULT 0,turn TEXT);
        CREATE TABLE IF NOT EXISTS sessions(project TEXT NOT NULL REFERENCES projects(id),thread TEXT NOT NULL,
          session TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,thread));
        CREATE UNIQUE INDEX IF NOT EXISTS unique_active_session ON projects(session) WHERE session IS NOT NULL;
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),
          update_id INTEGER UNIQUE,text TEXT,state TEXT NOT NULL,thread TEXT,turn TEXT UNIQUE,created REAL NOT NULL,started REAL,finished REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(project) WHERE state IN
          ('dispatching','running','waiting_approval','waiting_user','waiting_reference','unknown');
        CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS requests(token TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),
          thread TEXT NOT NULL,session TEXT NOT NULL,turn TEXT NOT NULL,rid TEXT NOT NULL,kind TEXT NOT NULL,
          payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',expires REAL NOT NULL,epoch TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL,acked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        PRAGMA user_version=1;
        """)

    def project(self, pid: str) -> dict:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise Rejected("Unknown project")
        return dict(row)

    def projects(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM projects ORDER BY id")]

    def setting(self, key: str, value: str | None = None):
        if value is not None:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            return value
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def add_project(self, p):
        old = self.db.execute("SELECT path FROM projects WHERE id=?", (p.id,)).fetchone()
        if old and old[0] != str(p.path):
            raise Rejected("Cannot change a registered project path")
        self.db.execute("INSERT INTO projects(id,name,path) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (p.id, p.name, str(p.path)))

    def bind(self, pid: str, thread: dict):
        self.db.execute("UPDATE projects SET thread=?,session=?,state='idle',turn=NULL WHERE id=?", (thread["id"], thread["sessionId"], pid))
        self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?)", (pid, thread["id"], thread["sessionId"], time.time()))

    def state(self, pid: str, state: str, turn: str | None = None):
        self.db.execute("UPDATE projects SET state=?,turn=? WHERE id=?", (state, turn, pid))

    def enqueue(self, pid: str, text: str, update: int | None) -> str:
        tid = uuid.uuid4().hex
        self.db.execute("INSERT INTO tasks(id,project,update_id,text,state,created) VALUES(?,?,?,?,'queued',?)", (tid, pid, update, text, time.time()))
        return tid

    def claim(self, pid: str) -> dict | None:
        p = self.project(pid)
        if p["state"] != "idle" or p["paused"] or self.active(pid):
            return None
        row = self.db.execute("SELECT * FROM tasks WHERE project=? AND state='queued' ORDER BY created,rowid LIMIT 1", (pid,)).fetchone()
        if row is None:
            return None
        changed = self.db.execute("UPDATE tasks SET state='dispatching',thread=?,started=? WHERE id=? AND state='queued'", (p["thread"], time.time(), row["id"])).rowcount
        return dict(row) if changed else None

    def active(self, pid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM tasks WHERE project=? AND state IN ('dispatching','running','waiting_approval','waiting_user','waiting_reference','unknown')", (pid,)).fetchone()
        return dict(r) if r else None

    def task_state(self, tid: str, state: str, turn: str | None = None):
        if state not in STATES:
            raise Rejected("Invalid task state")
        self.db.execute("UPDATE tasks SET state=?,turn=COALESCE(?,turn),text=CASE WHEN ? THEN NULL ELSE text END,finished=CASE WHEN ? THEN ? ELSE finished END WHERE id=?", (state, turn, state in TERMINAL, state in TERMINAL, time.time(), tid))

    def notice(self, body: dict):
        self.db.execute("INSERT INTO notices(body) VALUES(?)", (json.dumps(body, ensure_ascii=False),))

    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        self.db.execute("ROLLBACK")

    def recover(self):
        self.db.execute("UPDATE tasks SET state='unknown' WHERE state IN ('dispatching','running','waiting_approval','waiting_user','waiting_reference')")
        self.db.execute("UPDATE projects SET state='unknown' WHERE thread IS NOT NULL")
        self.db.execute("UPDATE requests SET state='stale',payload='{}' WHERE state IN ('pending','sending','editing')")

    def backup(self, path: Path):
        with sqlite3.connect(path) as target:
            self.db.backup(target)

    def prune(self, days: int = 30):
        cutoff = time.time() - days * 86400
        self.db.execute("DELETE FROM tasks WHERE finished<? AND state IN ('completed','failed','cancelled','interrupted')", (cutoff,))
        self.db.execute("DELETE FROM requests WHERE expires<? AND state IN ('stale','resolved')", (cutoff,))
        self.db.execute("DELETE FROM notices WHERE acked=1")
        # Update IDs remain durable so manual/replayed deliveries cannot rerun tasks.
