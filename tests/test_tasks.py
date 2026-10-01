import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class FakeClock:
    def __init__(self, start="2026-01-01T00:00:00+00:00"):
        self.dt = datetime.fromisoformat(start)

    def __call__(self):
        return self.dt.isoformat()

    def advance(self, seconds):
        self.dt += timedelta(seconds=seconds)


class TaskTakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.clock = FakeClock()
        self.service = Service(self.repo, lease_seconds=60, now_func=self.clock)
        self.item = self.service.create_item(
            {"title": "task item", "description": "investigation",
             "severity": "high", "quantity": 12, "threshold": 6},
            "creator", "dosimetrist")
        # 进入调查阶段（recorded -> reviewing -> investigation）
        self.service.transition(self.item["id"], "reviewing", 1, "officer", "radiation_officer")
        self.service.transition(self.item["id"], "investigation", 2, "officer", "radiation_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_task_created_on_investigation_entry(self):
        task = self.service.get_task(self.item["id"], "viewer")
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["assignee"])
        self.assertEqual(task["take_count"], 0)
        self.assertEqual(task["item_version_current"], self.item["version"] + 2)

    def test_no_task_before_investigation(self):
        item = self.service.create_item(
            {"title": "plain", "description": "x", "severity": "low", "quantity": 0, "threshold": 1},
            "creator", "dosimetrist")
        with self.assertRaises(NotFoundError):
            self.service.get_task(item["id"], "viewer")

    def test_take_requires_radiation_officer(self):
        for role in ("viewer", "dosimetrist", "health_physicist"):
            with self.assertRaises(PermissionDenied):
                self.service.take_task(self.item["id"], "u", role)
        task = self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["assignee"], "alice")

    def test_take_captures_evidence_summary_and_version(self):
        self.service.add_record(self.item["id"], {
            "kind": "evidence", "detail": "film badge reading", "status": "open",
            "external_ref": "EV-1"}, "recorder", "radiation_officer")
        task = self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["take_count"], 1)
        self.assertEqual(task["item_version"], task["item_version_current"])
        self.assertEqual(task["evidence_summary"]["record_count"], 1)
        self.assertEqual(task["evidence_summary"]["open_count"], 1)
        self.assertEqual(task["evidence_summary"]["latest"]["kind"], "evidence")
        self.assertIsNotNone(task["lease_expires_at"])

    def test_double_take_conflict(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.take_task(self.item["id"], "bob", "radiation_officer")

    def test_repeat_take_is_idempotent_and_keeps_first_task(self):
        first = self.service.take_task(self.item["id"], "alice", "radiation_officer")
        again = self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["take_count"], 1)
        self.assertEqual(again["assignee"], "alice")

    def test_lease_expiry_returns_to_pending_and_reuses_task(self):
        first = self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.service.checkpoint_task(self.item["id"], {"checkpoint": "cp1"}, "alice", "radiation_officer")
        # 模拟值班员调班/服务中断：租约超时
        self.clock.advance(120)
        task = self.service.get_task(self.item["id"], "viewer")
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["assignee"])
        # 接手人接办：沿用首次任务，从断点继续
        second = self.service.take_task(self.item["id"], "bob", "radiation_officer")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["take_count"], 2)
        self.assertEqual(second["assignee"], "bob")
        self.assertEqual(second["checkpoint"], "cp1")

    def test_checkpoint_persists_for_resume_after_failure(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.service.checkpoint_task(self.item["id"], {
            "checkpoint": "dose_reviewed",
            "data": {"note": "pending interview", "progress": 3}}, "alice", "radiation_officer")
        # 处理失败/中断，租约超时回到待接办
        self.clock.advance(120)
        bob = self.service.take_task(self.item["id"], "bob", "radiation_officer")
        self.assertEqual(bob["checkpoint"], "dose_reviewed")
        self.assertEqual(bob["checkpoint_data"]["progress"], 3)
        # 接手人从断点继续推进
        bob = self.service.checkpoint_task(self.item["id"], {
            "checkpoint": "interview_done", "data": {"progress": 5}}, "bob", "radiation_officer")
        self.assertEqual(bob["checkpoint"], "interview_done")
        self.assertEqual(bob["checkpoint_data"]["progress"], 5)

    def test_checkpoint_requires_holder(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.checkpoint_task(self.item["id"], {"checkpoint": "x"}, "u", "viewer")
        with self.assertRaises(ConflictError):
            self.service.checkpoint_task(self.item["id"], {"checkpoint": "x"}, "bob", "radiation_officer")
        # 待接办状态下也不能写断点
        self.clock.advance(120)
        with self.assertRaises(ConflictError):
            self.service.checkpoint_task(self.item["id"], {"checkpoint": "x"}, "bob", "radiation_officer")

    def test_stale_snapshot_after_event_change_keeps_checkpoint(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.service.checkpoint_task(self.item["id"], {"checkpoint": "cp1"}, "alice", "radiation_officer")
        # 事件改动（版本前进），旧快照失效
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute("UPDATE items SET version=version+1 WHERE id=?", (self.item["id"],))
        self.clock.advance(120)  # 租约超时回到待接办
        task = self.service.get_task(self.item["id"], "viewer")
        self.assertTrue(task["stale"])
        # 接手人接办：刷新证据快照与事件版本，但断点保留
        bob = self.service.take_task(self.item["id"], "bob", "radiation_officer")
        self.assertFalse(bob["stale"])
        self.assertEqual(bob["checkpoint"], "cp1")
        self.assertEqual(bob["item_version"], bob["item_version_current"])

    def test_complete_task_on_transition_out_of_investigation(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.service.transition(self.item["id"], "follow_up", 3, "physician", "health_physicist")
        done = self.service.get_task(self.item["id"], "viewer")
        self.assertEqual(done["status"], "done")
        self.assertIsNone(done["assignee"])
        with self.assertRaises(ConflictError):
            self.service.take_task(self.item["id"], "bob", "radiation_officer")

    def test_explicit_complete(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        done = self.service.complete_task(self.item["id"], "alice", "radiation_officer")
        self.assertEqual(done["status"], "done")

    def test_list_tasks_shows_visibility(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        tasks = self.service.list_tasks("viewer")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["assignee"], "alice")
        self.assertEqual(tasks[0]["item_title"], "task item")
        # 按状态过滤
        self.assertEqual(len(self.service.list_tasks("viewer", "pending")), 0)
        self.assertEqual(len(self.service.list_tasks("viewer", "active")), 1)

    def test_sweep_expired_returns_count(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.assertEqual(self.service.sweep_expired_tasks(), 0)
        self.clock.advance(120)
        self.assertEqual(self.service.sweep_expired_tasks(), 1)
        task = self.service.get_task(self.item["id"], "viewer")
        self.assertEqual(task["status"], "pending")

    def test_audit_trail_records_take_and_checkpoint(self):
        self.service.take_task(self.item["id"], "alice", "radiation_officer")
        self.service.checkpoint_task(self.item["id"], {"checkpoint": "cp1"}, "alice", "radiation_officer")
        events = self.service.audit("health_physicist", self.item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("task_take", actions)
        self.assertIn("task_checkpoint", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
