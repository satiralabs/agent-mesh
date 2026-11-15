"""Phase 1 tests. Runner: python3 -m unittest discover -s tests -v
(stdlib unittest — pytest is not installed in this VM.)

Covers: full task flow between two agents on a live node, invalid
transitions, dependency gating, idempotency, vault secrecy + ACL +
approval, 401 for unauthenticated calls, and a second independent node.
"""
import os
import shutil
import tempfile
import threading
import unittest

from agent_mesh.client import MeshClient, MeshError
from agent_mesh.node import Node, serve

ADMIN = "test-admin-token"
MASTER = "test-master-passphrase"
DUMMY_SECRET = "DUMMY-SECRET-VALUE-9f27c1-do-not-leak"


def start_node():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-test-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)  # ephemeral port
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, data_dir, f"http://127.0.0.1:{port}"


class NodeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        reg_a = cls.admin.register(name="Agent A", machine="vm",
                                   capabilities=["coding"], role="orchestrator")
        reg_b = cls.admin.register(name="Agent B", machine="vm",
                                   capabilities=["research"], role="worker")
        cls.a_id, cls.b_id = reg_a["agent"]["id"], reg_b["agent"]["id"]
        cls.a = MeshClient(cls.base, token=reg_a["token"])
        cls.b = MeshClient(cls.base, token=reg_b["token"])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def walk_task(self, client_creator, worker, title="task",
                    result="done-result", **kw):
        """Create a task and walk it to IN_REVIEW; returns task id."""
        out = client_creator.create_task(title=title, **kw)
        tid = out["task"]["id"]
        client_creator.transition(tid, "SENT")
        worker.transition(tid, "ACCEPTED")
        worker.transition(tid, "IN_PROGRESS")
        worker.progress(tid, "halfway")
        worker.transition(tid, "IN_REVIEW")
        return tid


class TestTaskFlow(NodeFixture):
    def test_full_flow_and_result_readback(self):
        tid = self.walk_task(self.a, self.b, title="Research X",
                             assignee_agent=self.b_id)
        # Reviewer/owner (A, the requester) marks DONE with a result.
        done = self.a.transition(tid, "DONE", result="verified: 42")
        self.assertEqual(done["task"]["status"], "DONE")
        readback = self.a.get_task(tid)["task"]
        self.assertEqual(readback["result"], "verified: 42")
        self.assertEqual(readback["requester"], self.a_id)
        self.assertEqual(readback["assignee_agent"], self.b_id)
        notes = readback["progress_notes"]
        self.assertTrue(any(n["note"] == "halfway" for n in notes))

    def test_messages_and_heartbeat(self):
        self.b.heartbeat("busy")
        agents = {x["id"]: x for x in self.a.list_agents()["agents"]}
        self.assertEqual(agents[self.b_id]["status"], "busy")
        self.assertNotIn("token_hash", agents[self.b_id])
        self.a.send_message(self.b_id, "INFO", "hello B")
        inbox = self.b.list_messages()["messages"]
        self.assertTrue(any(m["payload"] == "hello B" for m in inbox))

    def test_invalid_transition_rejected(self):
        out = self.a.create_task(title="bad jump", assignee_agent=self.b_id)
        tid = out["task"]["id"]
        with self.assertRaises(MeshError) as ctx:
            self.a.transition(tid, "DONE")  # DRAFT -> DONE is not allowed
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("DRAFT -> DONE", str(ctx.exception.body))
        self.assertEqual(self.a.get_task(tid)["task"]["status"], "DRAFT")

    def test_dependency_blocks_until_done(self):
        dep_id = self.walk_task(self.a, self.b, title="prerequisite",
                                assignee_agent=self.b_id)
        blocked = self.a.create_task(title="blocked task",
                                     assignee_agent=self.b_id,
                                     depends_on=dep_id)
        bid = blocked["task"]["id"]
        self.a.transition(bid, "SENT")
        with self.assertRaises(MeshError) as ctx:
            self.b.transition(bid, "ACCEPTED")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("must be DONE", str(ctx.exception.body))
        # Finish the dependency, then the blocked task may proceed.
        self.a.transition(dep_id, "DONE", result="prereq complete")
        accepted = self.b.transition(bid, "ACCEPTED")
        self.assertEqual(accepted["task"]["status"], "ACCEPTED")

    def test_idempotency_key_creates_one_task(self):
        kw = dict(title="pay once", assignee_agent=self.b_id,
                  idempotency_key="order-123")
        first = self.a.create_task(**kw)
        second = self.a.create_task(**kw)
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        mine = self.a.list_tasks(requester=self.a_id)["tasks"]
        matches = [t for t in mine if t["idempotency_key"] == "order-123"]
        self.assertEqual(len(matches), 1)


class TestVault(NodeFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin.vault_add_service(
            service="dummy", secret=DUMMY_SECRET,
            upstream="https://example.invalid/api",
            acl={cls.a_id: ["read", "write"]})
        cls.admin.vault_add_service(
            service="gated", secret="GATED-DUMMY-SECRET-55aa",
            upstream="https://example.invalid/gated",
            acl={cls.a_id: ["spend"]}, requires_approval=True)

    def test_authorised_allow_unauthorised_deny(self):
        self.assertEqual(self.a.vault_use_check("dummy", "read")["outcome"],
                         "allow")
        with self.assertRaises(MeshError) as ctx:
            self.b.vault_use_check("dummy", "read")  # B is not in the ACL
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body["outcome"], "deny")
        with self.assertRaises(MeshError) as ctx2:
            self.a.vault_use_check("dummy", "spend")  # action not granted
        self.assertEqual(ctx2.exception.status, 403)

    def test_requires_approval(self):
        out = self.a.vault_use_check("gated", "spend")
        self.assertEqual(out["outcome"], "needs_approval")

    def test_secret_never_leaks(self):
        # Not in any API response.
        listing = self.a.vault_services()
        self.assertNotIn(DUMMY_SECRET, str(listing))
        self.assertTrue(listing["services"]["dummy"]["has_secret"])
        # Not in any file under the node's data dir (incl. audit log, DBs).
        offenders = []
        for root, _dirs, files in os.walk(self.data_dir):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    if DUMMY_SECRET.encode() in fh.read():
                        offenders.append(path)
        self.assertEqual(offenders, [])
        # Audit log exists and records outcomes, sanitised.
        audit_path = os.path.join(self.data_dir, "audit.log")
        with open(audit_path) as fh:
            audit = fh.read()
        self.assertIn('"outcome": "allow"', audit)
        self.assertIn('"outcome": "deny"', audit)
        self.assertNotIn(DUMMY_SECRET, audit)


class TestAuth(NodeFixture):
    def test_unauthenticated_is_401(self):
        anon = MeshClient(self.base)  # no token at all
        with self.assertRaises(MeshError) as ctx:
            anon.list_agents()
        self.assertEqual(ctx.exception.status, 401)
        bad = MeshClient(self.base, token="wrong-token")
        with self.assertRaises(MeshError) as ctx2:
            bad.list_tasks()
        self.assertEqual(ctx2.exception.status, 401)

    def test_agent_cannot_do_admin_actions(self):
        with self.assertRaises(MeshError) as ctx:
            self.a.vault_add_service(service="evil", secret="x")
        self.assertEqual(ctx.exception.status, 401)


class TestSecondNodeIndependent(unittest.TestCase):
    def test_second_node_same_flow(self):
        server, data_dir, base = start_node()
        try:
            admin = MeshClient(base, admin_token=ADMIN)
            ra = admin.register(name="Solo A", role="orchestrator")
            rb = admin.register(name="Solo B", role="worker")
            a = MeshClient(base, token=ra["token"])
            b = MeshClient(base, token=rb["token"])
            tid = a.create_task(title="independent",
                                assignee_agent=rb["agent"]["id"])["task"]["id"]
            a.transition(tid, "SENT")
            b.transition(tid, "ACCEPTED")
            b.transition(tid, "IN_PROGRESS")
            b.transition(tid, "IN_REVIEW")
            done = a.transition(tid, "DONE", result="node2 ok")
            self.assertEqual(done["task"]["result"], "node2 ok")
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
