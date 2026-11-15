"""Phase 5 tests. Runner: python3 -m unittest discover -s tests -v
(stdlib unittest, same as Phases 1-4.)

Covers: the hard-enforced ordering matrix (local + remote-originated),
the secret scanner (block / prose pass / owner override / worker
override denied / file-drop), GUI auto-refresh statics, node data-file
permissions, the /messages inbox-scoping fix, and the 2.0.0 version.
"""
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from agent_mesh import __version__ as MESH_VERSION
from agent_mesh import peers as peers_mod
from agent_mesh.client import MeshClient, MeshError
from agent_mesh.filedrop import process_once
from agent_mesh.mcp_server import SERVER_INFO
from agent_mesh.node import Node, serve
from agent_mesh.scan import scan_text

ADMIN = "test-admin-token-p5"
MASTER = "test-master-passphrase-p5"
FAKE_GHP = "ghp_Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0LzXcVb"
PEM_BLOCK = ("-----BEGIN PRIVATE KEY-----\n"
             "MIIBvgIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA\n"
             "-----END PRIVATE KEY-----")


def start_node():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p5-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, node, data_dir, f"http://127.0.0.1:{port}"


def raw_admin_post(base, path, body, admin=ADMIN):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(body).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Admin-Token", admin)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def walk_files_for(data_dir, needle: bytes):
    offenders = []
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as fh:
                if needle in fh.read():
                    offenders.append(path)
    return offenders


class MatrixFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.node, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        reg = {}
        for key, role in (("boss", "owner"), ("orch", "orchestrator"),
                          ("w1", "worker"), ("w2", "worker")):
            reg[key] = cls.admin.register(name=f"P5 {key}", role=role)
        cls.ids = {k: v["agent"]["id"] for k, v in reg.items()}
        cls.clients = {k: MeshClient(cls.base, token=v["token"])
                       for k, v in reg.items()}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)


class TestOrderingMatrix(MatrixFixture):
    def test_worker_cannot_order_worker(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["w1"].create_task(
                title="worker ordering worker",
                assignee_agent=self.ids["w2"])
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("may not order", str(ctx.exception.body))
        with open(os.path.join(self.data_dir, "audit.log")) as fh:
            self.assertIn("task_order_denied", fh.read())

    def test_worker_cannot_order_by_capability(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["w1"].create_task(
                title="worker capability order",
                assignee_capability="coding")
        self.assertEqual(ctx.exception.status, 403)

    def test_worker_can_order_itself(self):
        out = self.clients["w1"].create_task(
            title="note to self", assignee_agent=self.ids["w1"])
        self.assertEqual(out["task"]["assignee_agent"], self.ids["w1"])

    def test_orchestrator_orders_worker(self):
        out = self.clients["orch"].create_task(
            title="orch to worker", assignee_agent=self.ids["w1"])
        self.assertEqual(out["task"]["status"], "DRAFT")

    def test_orchestrator_cannot_order_owner(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["orch"].create_task(
                title="orch ordering the boss",
                assignee_agent=self.ids["boss"])
        self.assertEqual(ctx.exception.status, 403)

    def test_owner_agent_orders_anyone(self):
        out = self.clients["boss"].create_task(
            title="boss to worker", assignee_agent=self.ids["w2"])
        self.assertEqual(out["task"]["assignee_agent"], self.ids["w2"])
        out = self.clients["boss"].create_task(
            title="boss by capability", assignee_capability="research")
        self.assertEqual(out["task"]["assignee_capability"], "research")

    def test_gui_owner_actor_orders_anyone(self):
        status, out = raw_admin_post(self.base, "/tasks", {
            "title": "owner gui order", "assignee_agent": self.ids["w1"]})
        self.assertEqual(status, 201)
        self.assertEqual(out["task"]["requester"], "owner")

    def test_default_role_is_orchestrator_and_can_order(self):
        reg = self.admin.register(name="P5 roleless")
        self.assertEqual(reg["agent"]["role"], "orchestrator")
        client = MeshClient(self.base, token=reg["token"])
        out = client.create_task(title="roleless orders worker",
                                 assignee_agent=self.ids["w1"])
        self.assertEqual(out["task"]["status"], "DRAFT")

    def test_messages_are_not_orders(self):
        out = self.clients["w1"].send_message(
            self.ids["w2"], "QUESTION", "can you check something?")
        self.assertIn("id", out)


class TestScanner(MatrixFixture):
    def test_message_with_github_token_blocked_and_nowhere_stored(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["orch"].send_message(
                self.ids["w1"], "INFO", f"here is the key: {FAKE_GHP}")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.body["rule"], "github_token")
        self.assertNotIn(FAKE_GHP, str(ctx.exception.body))
        self.assertEqual(
            walk_files_for(self.data_dir, FAKE_GHP.encode()), [])
        inbox = self.clients["w1"].list_messages()["messages"]
        self.assertNotIn(FAKE_GHP, json.dumps(inbox))

    def test_prose_mentioning_token_passes(self):
        out = self.clients["orch"].send_message(
            self.ids["w1"], "INFO",
            "let us discuss the token budget and the secret santa plan")
        self.assertIn("id", out)

    def test_task_instructions_with_pem_blocked(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["orch"].create_task(
                title="deploy", instructions=f"use this:\n{PEM_BLOCK}",
                assignee_agent=self.ids["w1"])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.body["rule"], "pem_private_key")
        titles = [t["title"] for t in
                  self.clients["orch"].list_tasks()["tasks"]]
        self.assertNotIn("deploy", titles)

    def test_assignment_blocked_placeholder_passes(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["orch"].create_task(
                title="creds", instructions="api_key = Sup3rSecretValue99",
                assignee_agent=self.ids["w1"])
        self.assertEqual(ctx.exception.body["rule"], "secret_assignment")
        out = self.clients["orch"].create_task(
            title="creds ok",
            instructions="set api_key = YOUR_API_KEY_HERE first",
            assignee_agent=self.ids["w1"])
        self.assertEqual(out["task"]["status"], "DRAFT")

    def test_owner_override_succeeds(self):
        status, out = raw_admin_post(self.base, "/tasks", {
            "title": "owner override task",
            "instructions": f"deliberate test value {FAKE_GHP}",
            "assignee_agent": self.ids["w1"], "allow_secret_risk": True})
        self.assertEqual(status, 201)
        self.assertEqual(out["task"]["title"], "owner override task")

    def test_worker_override_attempt_fails(self):
        with self.assertRaises(MeshError) as ctx:
            self.clients["w1"].create_task(
                title="worker override attempt",
                instructions=f"value {FAKE_GHP}",
                assignee_agent=self.ids["w1"], allow_secret_risk=True)
        self.assertEqual(ctx.exception.status, 403)
        mine = self.clients["w1"].list_tasks(
            requester=self.ids["w1"])["tasks"]
        self.assertNotIn("worker override attempt",
                         [t["title"] for t in mine])

    def test_scan_rules_unit(self):
        self.assertEqual(scan_text(f"key {FAKE_GHP}"), ["github_token"])
        self.assertIn("openai_key", scan_text(
            "sk-Ab3dEf5Gh7Jk9Mn1Pq3Rs5Tu7Vw9Xy"))
        self.assertIn("aws_access_key",
                      scan_text("id AKIAIOSFODNN7EXAMPLE end"))
        self.assertIn("jwt", scan_text(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"))
        self.assertIn("bearer_token", scan_text(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"))
        self.assertEqual(scan_text("the token economy and secret plans"),
                         [])
        self.assertEqual(scan_text("password = changeme-now"), [])
        self.assertIn("secret_assignment",
                      scan_text("password = Tr0ub4dor&3xyz"))

    def test_filedrop_payload_scanned_before_submit(self):
        drop = tempfile.mkdtemp(prefix="mesh-p5-drop-")
        self.addCleanup(shutil.rmtree, drop, True)
        with open(os.path.join(drop, "evil.json"), "w") as fh:
            json.dump({"kind": "message", "recipient": self.ids["w1"],
                       "msg_type": "INFO",
                       "payload": f"leak {FAKE_GHP}"}, fh)
        counts = process_once(drop, self.clients["orch"])
        self.assertEqual(counts, {"processed": 0, "failed": 1})
        err = os.path.join(drop, "failed", "evil.json.error.txt")
        with open(err) as fh:
            text = fh.read()
        self.assertIn("github_token", text)
        self.assertNotIn(FAKE_GHP, text)


class TestMessageScoping(MatrixFixture):
    def test_agent_cannot_read_another_agents_inbox(self):
        self.clients["orch"].send_message(
            self.ids["w1"], "INFO", "private-for-w1-eyes-only")
        stolen = self.clients["w2"].list_messages(
            recipient=self.ids["w1"])["messages"]
        self.assertNotIn("private-for-w1-eyes-only",
                         json.dumps(stolen))
        own = self.clients["w1"].list_messages()["messages"]
        self.assertIn("private-for-w1-eyes-only", json.dumps(own))


class TestGuiAutoRefresh(unittest.TestCase):
    def test_app_js_polls_current_screen_with_guards(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "agent_mesh", "web2", "app.js")) as fh:
            js = fh.read()
        with open(os.path.join(repo, "agent_mesh", "web2",
                               "index.html")) as fh:
            html = fh.read()
        self.assertIn("AUTO_REFRESH_MS = 10000", js)
        self.assertIn("setInterval(refreshCurrentScreen", js)
        self.assertIn("document.visibilityState", js)
        self.assertIn("SCREEN_REFRESH", js)
        self.assertIn("state.screen", js)
        self.assertIn("isEditing", js)
        self.assertIn('id="auto-refresh-indicator"', html)
        self.assertIn("Auto-refresh: on", html)


class TestFilePermissions(unittest.TestCase):
    def test_fresh_node_data_dir_and_files_are_owner_only(self):
        base = tempfile.mkdtemp(prefix="agent-mesh-p5-perms-")
        self.addCleanup(shutil.rmtree, base, True)
        data_dir = os.path.join(base, "node-data")
        os.makedirs(data_dir)
        os.chmod(data_dir, 0o755)  # Node must tighten this itself
        node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
        self.assertEqual(stat.S_IMODE(os.stat(data_dir).st_mode), 0o700)
        node.audit_event({"event": "perm_test", "agent": "owner",
                          "service": "", "action": "test",
                          "outcome": "ok"})
        from agent_mesh import bus
        bus.send_message("owner", "agt_x", "INFO", "hi",
                         db_path=node.bus_db)
        node.vault.set_service("permsvc", "a-secret-value", acl={})
        for name in ("agents.db", "tasks.db", "peers.db", "approvals.db",
                     "vault.json", "audit.log", "bus.db", "node.json"):
            mode = stat.S_IMODE(os.stat(os.path.join(data_dir, name)).st_mode)
            self.assertEqual(mode, 0o600, f"{name} is {oct(mode)}")


def peer_secret_from_db(data_dir, peer_node_id):
    conn = sqlite3.connect(os.path.join(data_dir, "peers.db"))
    row = conn.execute("SELECT secret FROM peers WHERE peer_node_id=?",
                       (peer_node_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def signed_post(base, path, payload, node_id, secret):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = peers_mod.sign(secret, ts, "POST", path, body)
    req = urllib.request.Request(base + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mesh-Node", node_id)
    req.add_header("X-Mesh-Timestamp", ts)
    req.add_header("X-Mesh-Signature", sig)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_for(pred, timeout=25, interval=0.3, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return
        except Exception:
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}")


class TestRemoteOrderingMatrix(unittest.TestCase):
    def test_remote_worker_order_rejected_remote_orchestrator_ok(self):
        srv_a, _na, dir_a, base_a = start_node()
        srv_b, _nb, dir_b, base_b = start_node()
        try:
            admin_a = MeshClient(base_a, admin_token=ADMIN)
            admin_b = MeshClient(base_b, admin_token=ADMIN)
            node_a = admin_a.node_info()["node_id"]
            a_worker = admin_a.register(name="A worker", role="worker")
            a_orch = admin_a.register(name="A orch", role="orchestrator")
            b_target = admin_b.register(name="B target",
                                        role="orchestrator")
            b_worker = admin_b.register(name="B worker", role="worker")
            code = admin_b.pairing_code()["code"]
            admin_a.pair_with(base_b, code)
            wait_for(lambda: any(
                a["id"] == a_worker["agent"]["id"] and a.get("remote")
                for a in admin_b.list_agents()["agents"]),
                desc="A's agents in B's directory")
            secret_b_for_a = peer_secret_from_db(dir_b, node_a)

            def snap(task_id, title, requester, assignee):
                now = time.time()
                return {"task": {
                    "id": task_id, "title": title, "instructions": "x",
                    "requester": requester, "assignee_agent": assignee,
                    "assignee_capability": None, "status": "SENT",
                    "priority": "normal", "progress_notes": [],
                    "result": None, "created_at": now, "updated_at": now}}

            # A worker on A ordering an agent on B: rejected, not created.
            status, out = signed_post(
                base_b, "/peer/task_sync",
                snap("tsk_p5violation", "Remote worker violation",
                     a_worker["agent"]["id"], b_target["agent"]["id"]),
                node_a, secret_b_for_a)
            self.assertEqual(status, 403)
            self.assertIn("may not order", out["error"])

            def tasks_on_b():
                req = urllib.request.Request(base_b + "/tasks",
                                             method="GET")
                req.add_header("X-Admin-Token", ADMIN)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())["tasks"]

            self.assertNotIn("Remote worker violation",
                             [t["title"] for t in tasks_on_b()])

            # An orchestrator on A ordering a worker on B: created.
            status, out = signed_post(
                base_b, "/peer/task_sync",
                snap("tsk_p5ok", "Remote orch order",
                     a_orch["agent"]["id"], b_worker["agent"]["id"]),
                node_a, secret_b_for_a)
            self.assertEqual(status, 200)
            self.assertIn("Remote orch order",
                          [t["title"] for t in tasks_on_b()])
        finally:
            srv_a.shutdown(); srv_a.server_close()
            srv_b.shutdown(); srv_b.server_close()
            shutil.rmtree(dir_a, ignore_errors=True)
            shutil.rmtree(dir_b, ignore_errors=True)


class TestVersion(unittest.TestCase):
    def test_version_is_2_0_0(self):
        # __init__ bump happens in the same phase; checked here so the
        # suite fails until every version surface agrees.
        self.assertEqual(MESH_VERSION, "2.0.0")
        self.assertEqual(SERVER_INFO["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
