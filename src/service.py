from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    TASK_CHECKPOINT_MAX, TASK_LEASE_SECONDS, TASK_PHASE,
                    TASK_TAKE_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, is_task_stale, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository, lease_seconds: int = TASK_LEASE_SECONDS,
                 now_func=None):
        self.repository = repository
        self.lease_seconds = max(1, int(lease_seconds))
        self._now = now_func or utc_now

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        self._sync_task_on_transition(item_id, item["status"], target, actor)
        return self.enrich(updated)

    def _sync_task_on_transition(self, item_id: int, current: str, target: str,
                                 actor: str) -> None:
        """事件进入调查即建立待接办任务；离开调查则办结；其余改动使在办失效。"""
        now = self._now()
        if target == TASK_PHASE:
            self.repository.create_task_if_needed(item_id, actor, now)
        elif current == TASK_PHASE:
            task = self.repository.complete_task(item_id, actor, now)
            self.repository.append_audit("task_complete", "task", task["id"], actor, {
                "item_id": item_id, "reason": "transition",
            })
        else:
            self.repository.release_task_for_item(item_id, now)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------------- 接办任务 ----------------
    def _enrich_task(self, task: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(task)
        result["stale"] = is_task_stale(task, item["version"])
        result["lease_seconds"] = self.lease_seconds
        result["item_version_current"] = item["version"]
        return result

    def get_task(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        task = self.repository.get_task(item_id, self._now())
        if task is None:
            from .domain import NotFoundError
            raise NotFoundError("该事件暂无接办任务")
        return self._enrich_task(task, item)

    def list_tasks(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        now = self._now()
        tasks = self.repository.list_tasks(status, now)
        result = []
        for task in tasks:
            item_version = task.get("item_version_current")
            task["stale"] = is_task_stale(task, item_version)
            task["lease_seconds"] = self.lease_seconds
            result.append(task)
        return result

    def take_task(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_TAKE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        task, code = self.repository.take_task(item_id, actor, self.lease_seconds, self._now())
        if code == "conflict":
            raise ConflictError("该事件已有人接办，请待租约超时或办结后再接办")
        if code == "done":
            raise ConflictError("调查已完成，任务已结束")
        self.repository.append_audit("task_take", "task", task["id"], actor, {
            "item_id": item_id, "take_count": task["take_count"],
            "item_version": task["item_version"], "code": code,
            "checkpoint": task.get("checkpoint"),
        })
        return self._enrich_task(task, item)

    def checkpoint_task(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_TAKE_ROLES)
        actor = require_text(actor, "actor", 100)
        checkpoint = require_text(payload.get("checkpoint"), "checkpoint", TASK_CHECKPOINT_MAX)
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            raise ValidationError("data必须是JSON对象")
        item = self.repository.get_item(item_id)
        task = self.repository.update_checkpoint(
            item_id, actor, checkpoint, data, self.lease_seconds, self._now())
        self.repository.append_audit("task_checkpoint", "task", task["id"], actor, {
            "item_id": item_id, "checkpoint": checkpoint,
        })
        return self._enrich_task(task, item)

    def complete_task(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_TAKE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        task = self.repository.complete_task(item_id, actor, self._now())
        self.repository.append_audit("task_complete", "task", task["id"], actor, {
            "item_id": item_id,
        })
        return self._enrich_task(task, item)

    def sweep_expired_tasks(self) -> int:
        return self.repository.sweep_expired(self._now())

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
