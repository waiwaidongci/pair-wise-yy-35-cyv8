import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib import request, error

from src.domain import ConflictError, PermissionDenied
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
from src.rules import STATES


def move_to_investigation(service, external_ref):
    item = service.create_item({
        "title": "dose event", "description": "over limit event",
        "severity": "high", "quantity": 20, "threshold": 5,
        "external_ref": external_ref,
    }, "creator", "dosimetrist")
    item = service.transition(item["id"], STATES[1], item["version"],
                              "officer-a", "radiation_officer")
    item = service.transition(item["id"], STATES[2], item["version"],
                              "officer-a", "radiation_officer")
    return item


class ClaimTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = move_to_investigation(self.service, "INV-1")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_only_radiation_officer_can_claim(self):
        for role in ("dosimetrist", "health_physicist", "viewer"):
            with self.assertRaises(PermissionDenied):
                self.service.claim_investigation(
                    self.item["id"], {"lease_seconds": 60}, "x", role)
        task = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        self.assertEqual(task["state"], "leased")
        self.assertEqual(task["claimed_by"], "officer-a")
        self.assertTrue(task["lease_valid"])

    def test_claim_freezes_evidence_and_version(self):
        self.service.add_record(self.item["id"], {
            "kind": "dosimetry", "detail": "finger ring 22mSv",
            "status": "open", "external_ref": "R-1",
        }, "officer-b", "radiation_officer")
        task = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        self.assertEqual(task["item_version"], self.item["version"])
        summary = task["evidence_summary"]
        self.assertEqual(summary["item_version"], self.item["version"])
        self.assertEqual(summary["quantity"], 20)
        self.assertEqual(summary["threshold"], 5)
        self.assertEqual(summary["open_records"], 1)
        self.assertEqual(summary["latest_records"][0]["external_ref"], "R-1")

    def test_duplicate_claim_reuses_first_task(self):
        first = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        again = self.service.claim_investigation(
            self.item["id"], {}, "officer-a", "radiation_officer")
        self.assertEqual(again["outcome"], "reused")
        self.assertEqual(again["task_id"], first["task_id"])

    def test_concurrent_claim_only_one_owner(self):
        outcomes = []
        barrier = threading.Barrier(2)

        def claim(actor):
            barrier.wait()
            try:
                task = self.service.claim_investigation(
                    self.item["id"], {"lease_seconds": 60}, actor,
                    "radiation_officer")
                outcomes.append(("ok", task["task_id"], actor))
            except ConflictError:
                outcomes.append(("conflict", None, actor))

        threads = [threading.Thread(target=claim, args=("officer-a",)),
                   threading.Thread(target=claim, args=("officer-b",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 2)
        oks = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len({o[1] for o in oks}), 1)
        task = self.service.investigation_task(self.item["id"], "viewer")
        self.assertEqual(task["claimed_by"], oks[0][2])

    def test_lease_expiry_returns_to_pending_and_task_reused(self):
        first = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 30}, "officer-a",
            "radiation_officer")
        self.service.investigation_checkpoint(self.item["id"], {
            "step": "measure-baseline", "note": "baseline collected",
            "lease_seconds": 30,
        }, "officer-a", "radiation_officer")
        # 模拟租约自然超时
        self.repo.conn.execute(
            "UPDATE investigation_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00'")
        self.repo.conn.commit()
        # 租约超时后自动回到待接办
        task = self.service.investigation_task(self.item["id"], "viewer")
        self.assertEqual(task["state"], "pending")
        self.assertFalse(task["lease_valid"])
        # 下一位接办沿用同一个任务，并从断点继续
        second = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-b",
            "radiation_officer")
        self.assertEqual(second["outcome"], "expired")
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["claimed_by"], "officer-b")
        self.assertEqual(second["last_claimed_by"], "officer-b")
        self.assertEqual(second["checkpoint"]["step"], "measure-baseline")
        self.assertEqual(second["attempts"], 2)

    def test_checkpoint_heartbeat_blocks_others(self):
        self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.investigation_checkpoint(self.item["id"], {
                "step": "x", "note": "y",
            }, "officer-b", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.claim_investigation(
                self.item["id"], {"lease_seconds": 60}, "officer-b",
                "radiation_officer")

    def test_leaving_investigation_completes_task(self):
        first = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        current = self.service.get_item(self.item["id"], "viewer")
        self.service.transition(current["id"], STATES[3], current["version"],
                                "physician", "health_physicist")
        done = self.service.investigation_task(self.item["id"], "viewer")
        self.assertEqual(done["state"], "completed")
        self.assertFalse(done["active"])
        self.assertEqual(done["task_id"], first["task_id"])

    def test_event_change_invalidates_old_task_and_successor_resumes(self):
        first = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        self.service.investigation_checkpoint(self.item["id"], {
            "step": "interview-worker", "note": "interview done",
            "lease_seconds": 60,
        }, "officer-a", "radiation_officer")
        # 事件被其他路径改动（如剂量更正），版本递增
        self.repo.conn.execute(
            "UPDATE items SET version=version+1, quantity=21 WHERE id=?",
            (self.item["id"],))
        self.repo.conn.commit()
        # 新一班辐射防护员接办：旧任务失效，新任务沿用断点
        successor = self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-b",
            "radiation_officer")
        self.assertEqual(successor["outcome"], "succeeded")
        self.assertNotEqual(successor["task_id"], first["task_id"])
        self.assertEqual(successor["item_version"], self.item["version"] + 1)
        self.assertEqual(successor["supersedes_task_id"], first["task_id"])
        self.assertEqual(successor["checkpoint"]["step"], "interview-worker")
        self.assertEqual(successor["evidence_summary"]["quantity"], 21)
        stale = self.repo.get_task(first["task_id"])
        self.assertEqual(stale["state"], "stale")

    def test_checkpoint_on_changed_event_is_rejected(self):
        self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        # 直接通过仓储制造版本漂移（例如其他路径改动了事件）
        self.repo.conn.execute(
            "UPDATE items SET version=version+1 WHERE id=?", (self.item["id"],))
        self.repo.conn.commit()
        with self.assertRaises(ConflictError):
            self.service.investigation_checkpoint(self.item["id"], {
                "step": "late-write", "note": "stale lease",
                "lease_seconds": 60,
            }, "officer-a", "radiation_officer")
        task = self.service.investigation_task(self.item["id"], "viewer")
        self.assertEqual(task["state"], "stale")

    def test_claim_non_investigation_item_conflicts(self):
        recorded = self.service.create_item({
            "title": "new", "description": "just recorded", "severity": "low",
            "quantity": 1, "threshold": 10,
        }, "creator", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.claim_investigation(
                recorded["id"], {"lease_seconds": 60}, "officer-a",
                "radiation_officer")

    def test_item_carries_active_task_summary(self):
        self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        self.service.investigation_checkpoint(self.item["id"], {
            "step": "sample-analysis", "note": "lab pending",
            "lease_seconds": 60,
        }, "officer-a", "radiation_officer")
        view = self.service.get_item(self.item["id"], "viewer")
        summary = view["investigation_task"]
        self.assertEqual(summary["claimed_by"], "officer-a")
        self.assertEqual(summary["checkpoint_step"], "sample-analysis")
        self.assertTrue(summary["lease_valid"])

    def test_audit_chain_intact(self):
        self.service.claim_investigation(
            self.item["id"], {"lease_seconds": 60}, "officer-a",
            "radiation_officer")
        self.service.investigation_checkpoint(self.item["id"], {
            "step": "s1", "note": "n1", "lease_seconds": 60,
        }, "officer-a", "radiation_officer")
        self.assertTrue(self.repo.verify_audit_chain())


class ClaimHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        service = Service(self.repo)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(service, static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        item = move_to_investigation(service, "HTTP-1")
        self.item_id = item["id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.repo.close()
        self.tmp.cleanup()

    def _post(self, path, role, actor="officer-a", payload=None):
        data = json.dumps(payload or {"lease_seconds": 60}).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Actor": actor, "X-Role": role})
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_other_roles_get_403(self):
        for role in ("dosimetrist", "health_physicist", "viewer"):
            status, body = self._post(
                f"/api/items/{self.item_id}/investigation/claim", role)
            self.assertEqual(status, 403, body)
        status, body = self._post(
            f"/api/items/{self.item_id}/investigation/claim",
            "radiation_officer")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "leased")

    def test_second_claim_gets_409(self):
        self._post(f"/api/items/{self.item_id}/investigation/claim",
                   "radiation_officer", "officer-a")
        status, _ = self._post(
            f"/api/items/{self.item_id}/investigation/claim",
            "radiation_officer", "officer-b")
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
