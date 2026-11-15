"""Phase 4 tests. Runner: python3 -m unittest discover -s tests -v
(stdlib unittest, same as Phases 1-3.)

Two real node PROCESSES on localhost (separate data dirs and ports,
fast outbox retry via env) prove: pairing (one-time code, no secret in
any response), HMAC transport security (bad signature / stale timestamp
/ peer credentials refused on admin routes), directory sync, a remote
task flowing A -> B -> DONE back on A, a remote message, and the
offline outbox (task queued while B is down, delivered exactly once
when B returns).
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_mesh import peers as peers_mod  # noqa: E402
from agent_mesh.client import MeshClient, MeshError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_A = "test-admin-token-p4-a"
ADMIN_B = "test-admin-token-p4-b"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_node(port, data_dir, name, admin):
    env = dict(os.environ)
    env.update({
        "AGENT_MESH_ADMIN": admin,
        "AGENT_MESH_MASTER": "test-master-passphrase-p4",
        "AGENT_MESH_FLUSH_SEC": "0.3",
        "AGENT_MESH_RETRY_SCHEDULE": "0.2,0.5,1,2",
        "PYTHONPATH": REPO,
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_mesh.node", "--host", "127.0.0.1",
         "--port", str(port), "--data-dir", data_dir, "--name", name],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return proc, base
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"node on {port} did not come up")


def stop_node(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_for(pred, timeout=25, interval=0.3, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}")


def all_tasks(base, admin):
    req = urllib.request.Request(base + "/tasks", method="GET")
    req.add_header("X-Admin-Token", admin)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")["tasks"]


def peer_secret_from_db(data_dir, peer_node_id):
    conn = sqlite3.connect(os.path.join(data_dir, "peers.db"))
    row = conn.execute("SELECT secret FROM peers WHERE peer_node_id=?",
                       (peer_node_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def signed_post(base, path, payload, node_id, secret, ts=None, sig=None):
    body = json.dumps(payload).encode()
    ts = str(int(time.time())) if ts is None else ts
    sig = peers_mod.sign(secret, ts, "POST", path, body) if sig is None else sig
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


class PairedFixture(unittest.TestCase):
    """Nodes A and B, paired, with agent A1 on A and B1 on B."""

    @classmethod
    def setUpClass(cls):
        cls.dir_a = tempfile.mkdtemp(prefix="agent-mesh-p4a-")
        cls.dir_b = tempfile.mkdtemp(prefix="agent-mesh-p4b-")
        cls.proc_a, cls.base_a = start_node(free_port(), cls.dir_a,
                                            "Node A", ADMIN_A)
        cls.proc_b, cls.base_b = start_node(free_port(), cls.dir_b,
                                            "Node B", ADMIN_B)
        cls.admin_a = MeshClient(cls.base_a, admin_token=ADMIN_A)
        cls.admin_b = MeshClient(cls.base_b, admin_token=ADMIN_B)
        cls.node_a = cls.admin_a.node_info()["node_id"]
        cls.node_b = cls.admin_b.node_info()["node_id"]
        reg_a = cls.admin_a.register(name="Agent A1", capabilities=["coding"],
                                     role="orchestrator")
        reg_b = cls.admin_b.register(name="Agent B1",
                                     capabilities=["research"], role="worker")
        cls.a1_id, cls.b1_id = reg_a["agent"]["id"], reg_b["agent"]["id"]
        cls.a1 = MeshClient(cls.base_a, token=reg_a["token"])
        cls.b1 = MeshClient(cls.base_b, token=reg_b["token"])
        # Pair: code from A, B initiates.
        cls.pair_code = cls.admin_a.pairing_code()["code"]
        cls.admin_b.pair_with(cls.base_a, cls.pair_code)
        wait_for(lambda: any(
            a["id"] == cls.b1_id and a.get("remote")
            and a.get("home_node") == cls.node_b
            for a in cls.admin_a.list_agents()["agents"]),
            desc="B1 in A's directory")
        wait_for(lambda: any(
            a["id"] == cls.a1_id and a.get("remote")
            for a in cls.admin_b.list_agents()["agents"]),
            desc="A1 in B's directory")

    @classmethod
    def tearDownClass(cls):
        stop_node(cls.proc_a)
        stop_node(cls.proc_b)
        shutil.rmtree(cls.dir_a, ignore_errors=True)
        shutil.rmtree(cls.dir_b, ignore_errors=True)


class TestPairing(PairedFixture):
    def test_both_sides_list_each_other_without_secret(self):
        for client, other in ((self.admin_a, self.node_b),
                              (self.admin_b, self.node_a)):
            out = client.peers_list()
            blob = json.dumps(out)
            self.assertIn(other, blob)
            self.assertNotIn("secret", blob)
            peer = [p for p in out["peers"] if p["node_id"] == other][0]
            self.assertEqual(peer["status"], "online")
            self.assertTrue(peer["url"].startswith("http://127.0.0.1:"))

    def test_code_cannot_be_reused_and_wrong_code_fails(self):
        with self.assertRaises(MeshError):
            self.admin_b.pair_with(self.base_a, self.pair_code)  # used
        fresh = self.admin_a.pairing_code()["code"]
        with self.assertRaises(MeshError):
            self.admin_b.pair_with(self.base_a, "DEADBEEF")  # wrong
        self.assertTrue(fresh)  # (the fresh code stays unused)

    def test_directory_marks_remote_agents(self):
        agents_a = self.admin_a.list_agents()["agents"]
        b1 = [a for a in agents_a if a["id"] == self.b1_id][0]
        self.assertTrue(b1["remote"])
        self.assertEqual(b1["home_node"], self.node_b)
        a1_local = [a for a in agents_a if a["id"] == self.a1_id][0]
        self.assertFalse(a1_local["remote"])
        self.assertEqual(a1_local["home_node"], self.node_a)


class TestPeerSecurity(PairedFixture):
    def _secret(self):
        return peer_secret_from_db(self.dir_a, self.node_b)

    def test_valid_signature_accepted(self):
        status, out = signed_post(self.base_a, "/peer/ping", {},
                                  self.node_b, self._secret())
        self.assertEqual(status, 200)
        self.assertEqual(out["node_id"], self.node_a)

    def test_bad_signature_rejected(self):
        status, _ = signed_post(self.base_a, "/peer/ping", {},
                                self.node_b, self._secret(), sig="0" * 64)
        self.assertIn(status, (401, 403))

    def test_stale_timestamp_rejected(self):
        old = str(int(time.time()) - 3600)
        status, _ = signed_post(self.base_a, "/peer/ping", {},
                                self.node_b, self._secret(), ts=old)
        self.assertIn(status, (401, 403))

    def test_unknown_node_rejected(self):
        status, _ = signed_post(self.base_a, "/peer/ping", {},
                                "nod_doesnotexist", self._secret())
        self.assertIn(status, (401, 403))

    def test_peer_credentials_cannot_call_admin_endpoints(self):
        # A perfectly signed peer request aimed at an admin route must
        # not authenticate: peer auth is valid for /peer/* only.
        body = json.dumps({"name": "Sneaky"}).encode()
        ts = str(int(time.time()))
        sig = peers_mod.sign(self._secret(), ts, "POST",
                             "/agents/register", body)
        req = urllib.request.Request(self.base_a + "/agents/register",
                                     data=body, method="POST")
        req.add_header("X-Mesh-Node", self.node_b)
        req.add_header("X-Mesh-Timestamp", ts)
        req.add_header("X-Mesh-Signature", sig)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 401)
        names = [a["name"] for a in self.admin_a.list_agents()["agents"]]
        self.assertNotIn("Sneaky", names)


class TestRemoteFlow(PairedFixture):
    def test_remote_task_completes_and_flows_back(self):
        out = self.a1.create_task(
            title="Remote research task", instructions="Check the thing.",
            assignee_agent=self.b1_id)
        task_id = out["task"]["id"]
        self.assertEqual(out["task"]["home_node"], self.node_b)
        self.a1.transition(task_id, "SENT")

        def b_copy():
            tasks = self.b1.list_tasks(assignee=self.b1_id)["tasks"]
            mine = [t for t in tasks if t["title"] == "Remote research task"]
            return mine[0] if mine else None

        wait_for(lambda: b_copy() is not None, desc="task delivered to B")
        b_task = b_copy()
        self.assertEqual(b_task["origin_node"], self.node_a)
        self.assertEqual(b_task["origin_task_id"], task_id)
        self.b1.transition(b_task["id"], "ACCEPTED")
        self.b1.transition(b_task["id"], "IN_PROGRESS")
        self.b1.progress(b_task["id"], "halfway there")
        self.b1.transition(b_task["id"], "IN_REVIEW")
        self.b1.transition(b_task["id"], "DONE", result="remote result text")

        def a_done():
            t = self.a1.get_task(task_id)["task"]
            return t["status"] == "DONE" \
                and t.get("result") == "remote result text" \
                and any(n["note"] == "halfway there"
                        for n in t["progress_notes"])
        wait_for(a_done, desc="A's copy DONE with result + progress")

    def test_remote_message_lands_in_home_inbox(self):
        self.a1.send_message(self.b1_id, "INFO", "hello across the mesh")

        def arrived():
            msgs = self.b1.list_messages(recipient=self.b1_id)["messages"]
            return any(m["payload"] == "hello across the mesh"
                       and m["sender"] == self.a1_id for m in msgs)
        wait_for(arrived, desc="remote message in B1's inbox on B")


class TestOfflineOutbox(unittest.TestCase):
    def test_task_queued_while_peer_down_delivers_once_on_return(self):
        dir_c = tempfile.mkdtemp(prefix="agent-mesh-p4c-")
        dir_d = tempfile.mkdtemp(prefix="agent-mesh-p4d-")
        port_c, port_d = free_port(), free_port()
        proc_c = proc_d = None
        try:
            proc_c, base_c = start_node(port_c, dir_c, "Node C", ADMIN_A)
            proc_d, base_d = start_node(port_d, dir_d, "Node D", ADMIN_B)
            admin_c = MeshClient(base_c, admin_token=ADMIN_A)
            admin_d = MeshClient(base_d, admin_token=ADMIN_B)
            reg_c = admin_c.register(name="Agent C1", role="orchestrator")
            reg_d = admin_d.register(name="Agent D1", role="worker")
            c1 = MeshClient(base_c, token=reg_c["token"])
            d1_id = reg_d["agent"]["id"]
            code = admin_d.pairing_code()["code"]
            admin_c.pair_with(base_d, code)
            wait_for(lambda: any(
                a["id"] == d1_id for a in admin_c.list_agents()["agents"]),
                desc="D1 in C's directory")

            # Peer goes down; the order must still be accepted and queued.
            stop_node(proc_d)
            proc_d = None
            out = c1.create_task(title="Offline queued task",
                                 assignee_agent=d1_id)
            task_id = out["task"]["id"]
            c1.transition(task_id, "SENT")
            wait_for(lambda: admin_c.peers_outbox()["counts"]["pending"] >= 1,
                     timeout=10, desc="outbox shows the task pending")
            items = admin_c.peers_outbox()["items"]
            self.assertTrue(any(
                i["kind"] == "task_sync" and i["state"] == "pending"
                for i in items))

            # Peer returns; the queue drains and the task lands exactly once.
            proc_d, base_d2 = start_node(port_d, dir_d, "Node D", ADMIN_B)
            self.assertEqual(base_d2, base_d)

            def on_d():
                tasks = all_tasks(base_d, ADMIN_B)
                return [t for t in tasks
                        if t["title"] == "Offline queued task"]
            wait_for(lambda: len(on_d()) == 1, timeout=30,
                     desc="queued task delivered to D exactly once")
            wait_for(lambda: admin_c.peers_outbox()["counts"]["pending"] == 0,
                     timeout=30, desc="C's outbox fully delivered")
        finally:
            if proc_c:
                stop_node(proc_c)
            if proc_d:
                stop_node(proc_d)
            shutil.rmtree(dir_c, ignore_errors=True)
            shutil.rmtree(dir_d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
