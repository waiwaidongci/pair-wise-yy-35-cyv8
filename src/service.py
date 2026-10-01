from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity,
                     NotFoundError, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CLAIM_ROLES, CREATE_ROLES, ENTITY,
                    DEFAULT_LEASE_SECONDS, INVESTIGATION_STATE, MAX_LEASE_SECONDS,
                    MIN_LEASE_SECONDS, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

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
        if item["status"] == INVESTIGATION_STATE:
            task = self.repository.latest_task(item_id)
            if task is not None and task["state"] in ("stale", "completed"):
                self.repository.append_audit(
                    "investigation_task_" + task["state"], ENTITY, item_id, actor, {
                        "task_id": task["id"], "reason": "event_changed",
                        "from_version": task["item_version"],
                        "to_version": updated["version"],
                    })
        return self.enrich(updated)

    def claim_investigation(self, item_id: int, payload: Dict[str, Any],
                            actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        lease_seconds = payload.get("lease_seconds", DEFAULT_LEASE_SECONDS)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            from .domain import ValidationError
            raise ValidationError("lease_seconds必须是整数")
        if lease_seconds < MIN_LEASE_SECONDS or lease_seconds > MAX_LEASE_SECONDS:
            from .domain import ValidationError
            raise ValidationError(
                f"lease_seconds必须在{MIN_LEASE_SECONDS}到{MAX_LEASE_SECONDS}之间")
        item = self.repository.get_item(item_id)
        if item["status"] != INVESTIGATION_STATE:
            raise ConflictError("只有调查中的事件可以接办")
        task, outcome = self.repository.claim_task(item_id, actor, lease_seconds)
        self.repository.append_audit("investigation_claim", ENTITY, item_id, actor, {
            "task_id": task["id"], "outcome": outcome,
            "item_version": task["item_version"],
            "lease_expires_at": task["lease_expires_at"],
            "resume_from_step": task["checkpoint_step"] or None,
            "supersedes_task_id": task.get("supersedes_task_id"),
        })
        return self.task_view(task, outcome)

    def investigation_checkpoint(self, item_id: int, payload: Dict[str, Any],
                                 actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        step = require_text(payload.get("step"), "step", 200)
        note = require_text(payload.get("note"), "note")
        lease_seconds = payload.get("lease_seconds", DEFAULT_LEASE_SECONDS)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            from .domain import ValidationError
            raise ValidationError("lease_seconds必须是整数")
        if lease_seconds < MIN_LEASE_SECONDS or lease_seconds > MAX_LEASE_SECONDS:
            from .domain import ValidationError
            raise ValidationError(
                f"lease_seconds必须在{MIN_LEASE_SECONDS}到{MAX_LEASE_SECONDS}之间")
        task = self.repository.get_active_task(item_id)
        if task is None:
            raise ConflictError("该事件没有可续办的接办任务，请先接办")
        updated = self.repository.update_checkpoint(
            task["id"], actor, step, note, lease_seconds)
        self.repository.append_audit("investigation_checkpoint", ENTITY, item_id, actor, {
            "task_id": updated["id"], "step": step,
            "item_version": updated["item_version"],
            "lease_expires_at": updated["lease_expires_at"],
        })
        return self.task_view(updated, "heartbeat")

    def investigation_task(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        task = self.repository.get_active_task(item_id)
        if task is not None:
            return self.task_view(task, "active")
        task = self.repository.latest_task(item_id)
        if task is None:
            raise NotFoundError("该事件还没有接办任务")
        return self.task_view(task, task["state"])

    @staticmethod
    def task_view(task: Dict[str, Any], outcome: str) -> Dict[str, Any]:
        active = task["state"] in ("pending", "leased")
        now = utc_now()
        lease_valid = (active and task["state"] == "leased"
                       and bool(task["lease_expires_at"])
                       and task["lease_expires_at"] > now)
        return {
            "task_id": task["id"],
            "item_id": task["item_id"],
            "state": task["state"],
            "active": active,
            "outcome": outcome,
            "claimed_by": task["claimed_by"],
            "last_claimed_by": task["last_claimed_by"],
            "item_version": task["item_version"],
            "attempts": task["attempts"],
            "lease_seconds": task["lease_seconds"],
            "leased_at": task["leased_at"],
            "lease_expires_at": task["lease_expires_at"],
            "lease_valid": lease_valid,
            "supersedes_task_id": task["supersedes_task_id"],
            "checkpoint": {
                "step": task["checkpoint_step"] or None,
                "note": task["checkpoint_note"] or None,
                "at": task["checkpoint_at"],
            },
            "evidence_summary": task["evidence_summary"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        }

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

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        if item["status"] == INVESTIGATION_STATE:
            result["investigation_task"] = self._active_task_summary(item["id"])
        return result

    def _active_task_summary(self, item_id: int) -> Optional[Dict[str, Any]]:
        task = self.repository.get_active_task(item_id)
        if task is None:
            return None
        now = utc_now()
        return {
            "task_id": task["id"],
            "state": task["state"],
            "claimed_by": task["claimed_by"],
            "item_version": task["item_version"],
            "lease_expires_at": task["lease_expires_at"],
            "lease_valid": (task["state"] == "leased"
                            and bool(task["lease_expires_at"])
                            and task["lease_expires_at"] > now),
            "checkpoint_step": task["checkpoint_step"] or None,
            "checkpoint_at": task["checkpoint_at"],
            "attempts": task["attempts"],
        }
