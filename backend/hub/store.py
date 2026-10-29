"""
Persistent task history, execution logs and saved workflows (SQLite).

Every event is redacted BEFORE it is written, so exported logs never contain API keys.
A "workflow" is a named list of natural-language steps that can be replayed through the
agent (each step runs as a normal task, so permissions + approvals still apply).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT, status TEXT NOT NULL,
  created REAL NOT NULL, finished REAL, summary TEXT, workflow_id TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts REAL NOT NULL,
  type TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS workflows(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, steps TEXT NOT NULL, created REAL NOT NULL);
"""

MAX_EVENTS_PER_TASK = 2000


class TaskStore:
    def __init__(self, path: Path | str, redact: Callable[[Any], Any]):
        self._redact = redact
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            # a crash can leave tasks "running" forever — mark them interrupted
            self._db.execute("UPDATE tasks SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
            self._db.commit()

    # ── tasks ──
    def create_task(self, text: str, source: str = "text", workflow_id: str = "") -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks(id,text,source,status,created,workflow_id) VALUES(?,?,?,?,?,?)",
                (tid, self._redact(text), source, "running", time.time(), workflow_id or None))
            self._db.commit()
        return tid

    def finish_task(self, tid: str, status: str, summary: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE tasks SET status=?, finished=?, summary=? WHERE id=?",
                             (status, time.time(), self._redact(summary or "")[:2000], tid))
            self._db.commit()

    def add_event(self, tid: str, etype: str, data: Dict[str, Any]) -> None:
        if not tid:
            return
        payload = json.dumps(self._redact(data), ensure_ascii=False, default=str)
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM events WHERE task_id=?", (tid,)).fetchone()[0]
            if n >= MAX_EVENTS_PER_TASK:
                return
            self._db.execute("INSERT INTO events(task_id,ts,type,data) VALUES(?,?,?,?)",
                             (tid, time.time(), etype, payload))
            self._db.commit()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, tid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                return None
            events = self._db.execute("SELECT ts,type,data FROM events WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        task = dict(row)
        task["events"] = [{"ts": e["ts"], "type": e["type"], "data": json.loads(e["data"])} for e in events]
        return task

    def export_all(self, limit: int = 1000) -> Dict[str, Any]:
        tasks = [self.get_task(t["id"]) for t in self.list_tasks(limit)]
        return {"exported_at": time.time(), "tasks": tasks}

    def delete_task(self, tid: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM events WHERE task_id=?", (tid,))
            cur = self._db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            self._db.commit()
            return cur.rowcount > 0

    def clear_history(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM events")
            self._db.execute("DELETE FROM tasks")
            self._db.commit()

    # ── workflows ──
    def save_workflow(self, name: str, steps: List[str]) -> str:
        steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
        name = (name or "").strip()[:80]
        if not name or not steps or len(steps) > 30:
            raise ValueError("a workflow needs a name and 1–30 non-empty steps")
        wid = uuid.uuid4().hex[:10]
        with self._lock:
            self._db.execute("INSERT INTO workflows(id,name,steps,created) VALUES(?,?,?,?)",
                             (wid, name, json.dumps(self._redact(steps), ensure_ascii=False), time.time()))
            self._db.commit()
        return wid

    def list_workflows(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM workflows ORDER BY created DESC").fetchall()
        return [{"id": r["id"], "name": r["name"], "steps": json.loads(r["steps"]), "created": r["created"]} for r in rows]

    def get_workflow(self, wid: str) -> Optional[Dict[str, Any]]:
        return next((w for w in self.list_workflows() if w["id"] == wid), None)

    def delete_workflow(self, wid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM workflows WHERE id=?", (wid,))
            self._db.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
