from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL UNIQUE
                        REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','active','done')),
                    assignee TEXT,
                    lease_expires_at TEXT,
                    evidence_summary TEXT,
                    item_version INTEGER,
                    checkpoint TEXT,
                    checkpoint_data TEXT,
                    take_count INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
        # 迁移：已处于调查阶段但尚未建立接办任务的事件，补建待接办任务
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (item_id, status, take_count, created_by, created_at, updated_at)
                   SELECT id, 'pending', 0, 'system', ?, ? FROM items
                   WHERE status='investigation'""",
                (utc_now(), utc_now()),
            )

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---------------- 接办任务 ----------------
    @staticmethod
    def _task(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for field in ("evidence_summary", "checkpoint_data"):
            raw = data.get(field)
            if isinstance(raw, str) and raw:
                try:
                    data[field] = json.loads(raw)
                except json.JSONDecodeError:
                    data[field] = None
            else:
                data[field] = None
        return data

    @staticmethod
    def _lease_expiry(now: str, lease_seconds: int) -> str:
        dt = datetime.fromisoformat(now)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()

    def _expire_tasks(self, now: str, item_id: Optional[int] = None) -> None:
        sql = ("UPDATE tasks SET status='pending', assignee=NULL, lease_expires_at=NULL, "
               "updated_at=? WHERE status='active' AND lease_expires_at IS NOT NULL "
               "AND lease_expires_at < ?")
        params: tuple = (now, now)
        if item_id is not None:
            sql += " AND item_id=?"
            params = (now, now, item_id)
        self.conn.execute(sql, params)

    def _evidence_summary(self, item_id: int) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT kind, detail, status, created_by, created_at FROM records "
            "WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        records = [dict(r) for r in rows]
        open_records = [r for r in records if r["status"] == "open"]
        latest = records[-1] if records else None
        return {
            "record_count": len(records),
            "open_count": len(open_records),
            "kinds": sorted({r["kind"] for r in records}),
            "latest": (None if latest is None else {
                "kind": latest["kind"], "detail": latest["detail"][:200],
                "created_by": latest["created_by"], "created_at": latest["created_at"],
            }),
        }

    def create_task_if_needed(self, item_id: int, actor: str, now: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (item_id, status, take_count, created_by, created_at, updated_at)
                   VALUES(?, 'pending', 0, ?, ?, ?)""",
                (item_id, actor, now, now),
            )
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
        return self._task(row)

    def get_task(self, item_id: int, now: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = now or utc_now()
        with self._lock, self.conn:
            self._expire_tasks(now, item_id)
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
        return self._task(row) if row else None

    def list_tasks(self, status: Optional[str] = None,
                   now: Optional[str] = None) -> List[Dict[str, Any]]:
        now = now or utc_now()
        with self._lock, self.conn:
            self._expire_tasks(now)
            sql = ("SELECT t.*, i.title AS item_title, i.status AS item_status, "
                   "i.version AS item_version_current "
                   "FROM tasks t JOIN items i ON i.id=t.item_id")
            params: tuple = ()
            if status:
                sql += " WHERE t.status=?"
                params = (status,)
            sql += " ORDER BY t.id DESC"
            rows = self.conn.execute(sql, params).fetchall()
        return [self._task(row) for row in rows]

    def take_task(self, item_id: int, actor: str, lease_seconds: int, now: str
                  ) -> Tuple[Dict[str, Any], str]:
        """接办/续租。返回 (task, code)，code ∈ taken/renewed/conflict/done。"""
        lease_seconds = max(1, int(lease_seconds))
        with self._lock, self.conn:
            self._expire_tasks(now, item_id)
            item_row = self.conn.execute(
                "SELECT version FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("项目不存在")
            item_version = int(item_row["version"])
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO tasks(item_id, status, take_count, created_by, created_at, updated_at)
                       VALUES(?, 'pending', 0, ?, ?, ?)""",
                    (item_id, actor, now, now))
                row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
            task = dict(row)
            if task["status"] == "done":
                return self._task(row), "done"
            if task["status"] == "active":
                if task["assignee"] == actor:
                    # 同一人重复接办：续租；事件已改动则刷新证据快照，断点保留
                    evidence = task["evidence_summary"]
                    snap_version = task["item_version"]
                    if snap_version != item_version:
                        evidence = json.dumps(self._evidence_summary(item_id),
                                              ensure_ascii=False, sort_keys=True)
                        snap_version = item_version
                    lease = self._lease_expiry(now, lease_seconds)
                    self.conn.execute(
                        """UPDATE tasks SET lease_expires_at=?, evidence_summary=?,
                           item_version=?, updated_at=?
                           WHERE item_id=? AND status='active' AND assignee=?""",
                        (lease, evidence, snap_version, now, item_id, actor))
                    row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
                    return self._task(row), "renewed"
                return self._task(row), "conflict"
            # pending -> 接办：留下证据摘要与事件版本，沿用同一任务（断点保留）
            evidence = json.dumps(self._evidence_summary(item_id), ensure_ascii=False, sort_keys=True)
            lease = self._lease_expiry(now, lease_seconds)
            cur = self.conn.execute(
                """UPDATE tasks SET status='active', assignee=?, lease_expires_at=?,
                   evidence_summary=?, item_version=?, take_count=take_count+1, updated_at=?
                   WHERE item_id=? AND status='pending'""",
                (actor, lease, evidence, item_version, now, item_id))
            if cur.rowcount == 0:
                row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
                return self._task(row), "conflict"
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
        return self._task(row), "taken"

    def update_checkpoint(self, item_id: int, actor: str, checkpoint: str,
                          data: Optional[Dict[str, Any]], lease_seconds: int,
                          now: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            self._expire_tasks(now, item_id)
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("接办任务不存在")
            task = dict(row)
            if task["status"] == "done":
                raise ConflictError("调查已完成，任务已结束")
            if task["status"] != "active":
                raise ConflictError("任务待接办，请先接办后再记录断点")
            if task["assignee"] != actor:
                raise ConflictError("该任务正由其他值班员接办")
            lease = self._lease_expiry(now, max(1, int(lease_seconds)))
            if data is not None:
                data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                self.conn.execute(
                    """UPDATE tasks SET checkpoint=?, checkpoint_data=?, lease_expires_at=?, updated_at=?
                       WHERE item_id=? AND status='active' AND assignee=?""",
                    (checkpoint, data_json, lease, now, item_id, actor))
            else:
                self.conn.execute(
                    """UPDATE tasks SET checkpoint=?, lease_expires_at=?, updated_at=?
                       WHERE item_id=? AND status='active' AND assignee=?""",
                    (checkpoint, lease, now, item_id, actor))
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
        return self._task(row)

    def complete_task(self, item_id: int, actor: str, now: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            self._expire_tasks(now, item_id)
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("接办任务不存在")
            task = dict(row)
            if task["status"] == "done":
                return self._task(row)
            self.conn.execute(
                "UPDATE tasks SET status='done', assignee=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE item_id=?", (now, item_id))
            row = self.conn.execute("SELECT * FROM tasks WHERE item_id=?", (item_id,)).fetchone()
        return self._task(row)

    def release_task_for_item(self, item_id: int, now: str) -> bool:
        """事件改动后，使在办任务失效并回到待接办（断点保留）。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE tasks SET status='pending', assignee=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE item_id=? AND status='active'""",
                (now, item_id))
            return cur.rowcount > 0

    def sweep_expired(self, now: str) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE tasks SET status='pending', assignee=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE status='active' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?""",
                (now, now))
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self.conn.close()
