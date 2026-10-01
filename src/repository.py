from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import lease_expiry, make_entry, utc_now
from .domain import ConflictError, NotFoundError, PermissionDenied
from .rules import (ACTIVE_TASK_STATES, EVIDENCE_SUMMARY_LIMIT, STATES,
                    TASK_STATES)


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
        task_statuses = ",".join("'" + s + "'" for s in TASK_STATES)
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
                CREATE TABLE IF NOT EXISTS investigation_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    state TEXT NOT NULL CHECK(state IN ({task_statuses})),
                    claimed_by TEXT,
                    last_claimed_by TEXT,
                    item_version INTEGER NOT NULL,
                    evidence_summary TEXT NOT NULL,
                    checkpoint_step TEXT NOT NULL DEFAULT '',
                    checkpoint_note TEXT NOT NULL DEFAULT '',
                    checkpoint_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_seconds INTEGER NOT NULL,
                    leased_at TEXT,
                    lease_expires_at TEXT,
                    supersedes_task_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_active_task
                    ON investigation_tasks(item_id)
                    WHERE state IN ('pending','leased');
                CREATE INDEX IF NOT EXISTS ix_tasks_item ON investigation_tasks(item_id, id);
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
            """)

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
            before = self.conn.execute(
                "SELECT status, version FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if before is None:
                raise NotFoundError("项目不存在")
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
            # 事件版本发生变化：旧的有效调查任务失效；随调查正常转出的任务记为完成
            if before["status"] == "investigation":
                self.conn.execute(
                    """UPDATE investigation_tasks SET state=?, updated_at=?
                       WHERE item_id=? AND state IN ('pending','leased')""",
                    ("completed" if target != "investigation" else "stale", now, item_id),
                )
            else:
                self.conn.execute(
                    """UPDATE investigation_tasks SET state='stale', updated_at=?
                       WHERE item_id=? AND state IN ('pending','leased')""",
                    (now, item_id),
                )
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

    # ------------------------------------------------------------------
    # 调查接办任务
    # ------------------------------------------------------------------
    def build_evidence_summary(self, item: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT id, kind, detail, status, external_ref, created_by, created_at
                   FROM records WHERE item_id=? ORDER BY id DESC LIMIT ?""",
                (item["id"], EVIDENCE_SUMMARY_LIMIT),
            ).fetchall()
        recent = [dict(row) for row in rows]
        recent.reverse()
        return {
            "item_version": item["version"],
            "status": item["status"],
            "title": item["title"],
            "severity": item["severity"],
            "quantity": item["quantity"],
            "threshold": item["threshold"],
            "open_records": self.open_record_count(item["id"]),
            "latest_records": recent,
            "snapshot_at": utc_now(),
        }

    @staticmethod
    def _task(row: sqlite3.Row) -> Dict[str, Any]:
        task = dict(row)
        task["evidence_summary"] = json.loads(task["evidence_summary"])
        return task

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM investigation_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("接办任务不存在")
        return self._task(row)

    def get_active_task(self, item_id: int, sweep_expired: bool = True) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self._lock, self.conn:
            if sweep_expired:
                self.conn.execute(
                    """UPDATE investigation_tasks SET state='pending', updated_at=?
                       WHERE item_id=? AND state='leased' AND lease_expires_at<=?""",
                    (now, item_id, now),
                )
            row = self.conn.execute(
                """SELECT * FROM investigation_tasks WHERE item_id=?
                   AND state IN ('pending','leased') ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        return self._task(row) if row else None

    def latest_task(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM investigation_tasks WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return self._task(row) if row else None

    def claim_task(self, item_id: int, actor: str, lease_seconds: int,
                   now: Optional[str] = None) -> Dict[str, Any]:
        """原子接办：返回 (task, outcome)。

        outcome: claimed=新接办, reused=本人重复接办沿用首次任务,
                 expired=租约超时后重新接办, succeeded=从失效任务的断点接续
        """
        now = now or utc_now()
        with self._lock, self.conn:
            item = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            item = dict(item)

            active = self.conn.execute(
                """SELECT * FROM investigation_tasks WHERE item_id=?
                   AND state IN ('pending','leased') ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
            predecessor = None
            outcome = "claimed"
            if active is not None:
                active = dict(active)
                # 事件版本已变化：即使租约未到期，旧任务也立即失效，由新任务从断点接续
                if active["item_version"] != item["version"]:
                    self.conn.execute(
                        "UPDATE investigation_tasks SET state='stale', updated_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                    predecessor = active
                    outcome = "succeeded"
                elif active["state"] == "leased":
                    if active["lease_expires_at"] > now:
                        # 租约有效：只有原处理人可以沿用首次任务
                        if active["claimed_by"] == actor:
                            self.conn.execute(
                                """UPDATE investigation_tasks
                                   SET lease_seconds=?, lease_expires_at=?, updated_at=?
                                   WHERE id=?""",
                                (lease_seconds, lease_expiry(now, lease_seconds),
                                 now, active["id"]),
                            )
                            return self.get_task(active["id"]), "reused"
                        raise ConflictError("该事件已有有效接办任务，租约尚未到期")
                    # 租约超时：回到待接办，同一任务被下一人接办
                    self.conn.execute(
                        "UPDATE investigation_tasks SET state='pending', updated_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                    active["state"] = "pending"

                if predecessor is None:
                    was_leased_before = bool(active.get("last_claimed_by")) or active["attempts"] > 0
                    evidence = self.build_evidence_summary(item)
                    self.conn.execute(
                        """UPDATE investigation_tasks
                           SET state='leased', claimed_by=?, last_claimed_by=?,
                               item_version=?, evidence_summary=?, attempts=attempts+1,
                               leased_at=?, lease_expires_at=?, lease_seconds=?, updated_at=?
                           WHERE id=?""",
                        (actor, actor, item["version"],
                         json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                         now, lease_expiry(now, lease_seconds), lease_seconds,
                         now, active["id"]),
                    )
                    return self.get_task(active["id"]), (
                        "expired" if was_leased_before else "claimed")
            else:
                # 无有效任务：检查最后一个任务是否因事件改动/流转而终止
                last = self.conn.execute(
                    "SELECT * FROM investigation_tasks WHERE item_id=? ORDER BY id DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
                if last is not None:
                    predecessor = dict(last)
                    outcome = "succeeded" if predecessor["state"] in (
                        "stale", "completed") else "claimed"

            evidence = self.build_evidence_summary(item)
            cur = self.conn.execute(
                """INSERT INTO investigation_tasks(item_id, state, claimed_by,
                   last_claimed_by, item_version, evidence_summary, checkpoint_step,
                   checkpoint_note, checkpoint_at, attempts, lease_seconds, leased_at,
                   lease_expires_at, supersedes_task_id, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, "leased", actor, actor, item["version"],
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                 predecessor["checkpoint_step"] if predecessor else "",
                 predecessor["checkpoint_note"] if predecessor else "",
                 predecessor["checkpoint_at"] if predecessor else None,
                 1, lease_seconds, now, lease_expiry(now, lease_seconds),
                 predecessor["id"] if predecessor else None, now, now),
            )
        return self.get_task(int(cur.lastrowid)), outcome

    def update_checkpoint(self, task_id: int, actor: str, step: str, note: str,
                          lease_seconds: int, now: Optional[str] = None) -> Dict[str, Any]:
        now = now or utc_now()

        def _mark(state: str) -> None:
            with self._lock, self.conn:
                self.conn.execute(
                    "UPDATE investigation_tasks SET state=?, updated_at=? WHERE id=?",
                    (state, now, task_id),
                )

        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM investigation_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("接办任务不存在")
            task = dict(row)
            item = self.conn.execute(
                "SELECT version FROM items WHERE id=?", (task["item_id"],)
            ).fetchone()
        if item is None:
            raise NotFoundError("项目不存在")
        if task["state"] == "pending":
            raise ConflictError("任务尚未接办，请先接办")
        if task["state"] in ("stale", "completed"):
            raise ConflictError("任务已失效或完成，请重新接办后继续")
        if task["claimed_by"] != actor:
            raise PermissionDenied("只有当前接办人可以更新处理断点")
        if task["lease_expires_at"] <= now:
            _mark("pending")  # 租约超时：任务回到待接办（独立提交，不随冲突回滚）
            raise ConflictError("租约已超时，任务回到待接办，请重新接办")
        if item["version"] != task["item_version"]:
            _mark("stale")  # 事件已改动：旧任务失效（独立提交，不随冲突回滚）
            raise ConflictError("事件已发生改动，任务已失效，请重新接办")
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE investigation_tasks SET checkpoint_step=?, checkpoint_note=?,
                   checkpoint_at=?, lease_expires_at=?, lease_seconds=?, updated_at=?
                   WHERE id=?""",
                (step, note, now, lease_expiry(now, lease_seconds), lease_seconds,
                 now, task_id),
            )
        return self.get_task(task_id)

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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
