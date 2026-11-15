"""Phase 9 tests — v2.2 collaboration. Runner:
python3 -m unittest discover -s tests -v (stdlib unittest.)

Covers: task participants (the assignee is the lead; collaborators
work the same task together), involvement-scoped visibility, the
shared comment thread with its permission + scanner rules, lead-only
transitions, the MCP comment tool, and a two-node federation
round-trip where the collaborator lives on the peer node.
"""
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_mesh.client import MeshClient, MeshError  # noqa: E402
from agent_mesh.node import Node, serve  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = "test-admin-token-p9"
MASTER = "test-master-passphrase-p9"
# Same credential shape the Phase 5 scanner tests use (a fake token
# that must be blocked, and must never be stored): ghp_ + 36 chars.
FAKE_GHP = "ghp_" + "Ab3dEf6h" * 4 + "Ab3d"


def start_node_inprocess():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p9-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, data_dir, f"http://127.0.0.1:{port}"


def roles_by_id(task):
    return {p["agent_id"]: p["role"] for p in task["participants"]}


def raw_admin(base, method, path, body=None, admin=ADMIN):
    """Admin call the way the GUI makes it: X-Admin-Token header.
    (MeshClient only sends its admin token on admin=True paths, and
    its register() adopts the registered agent's token — neither fits
    a plain 'act as owner' call.) Returns (status, parsed_json)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("X-Admin-Token", admin)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class CollabFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.data_dir, cls.base = start_node_inprocess()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)

        cls.tokens = {}

        def reg(name, role):
            out = cls.admin.register(name=name, role=role)
            cls.tokens[out["agent"]["id"]] = out["token"]
            return out["agent"]["id"], MeshClient(cls.base,
                                                  token=out["token"])

        cls.req_id, cls.req = reg("Requester", "orchestrator")
        cls.lead_id, cls.lead = reg("Lead", "worker")
        cls.collab_id, cls.collab = reg("Collab", "worker")
        cls.collab2_id, cls.collab2 = reg("Collab Two", "orchestrator")
        cls.out_id, cls.outsider = reg("Outsider", "worker")
        cls.boss_id, cls.boss = reg("Boss", "owner")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def make_task(self, participants=None, title="Shared task"):
        kw = dict(title=title, instructions="work together",
                  assignee_agent=self.lead_id)
        if participants:
            kw["participants"] = participants
        out = self.req.create_task(**kw)
        tid = out["task"]["id"]
        self.req.transition(tid, "SENT")
        return tid

    def walk_to_done(self, tid):
        self.lead.transition(tid, "ACCEPTED")
        self.lead.transition(tid, "IN_PROGRESS")
        self.lead.transition(tid, "IN_REVIEW")
        self.req.transition(tid, "DONE", result="shipped")


class TestParticipantsAndVisibility(CollabFixture):
    def test_create_with_participants_roles(self):
        out = self.req.create_task(
            title="Team task", assignee_agent=self.lead_id,
            participants=[self.collab_id])
        task = out["task"]
        self.assertEqual(roles_by_id(task),
                         {self.lead_id: "lead",
                          self.collab_id: "collaborator"})
        # Lead and requester listed as participants are not duplicated.
        out2 = self.req.create_task(
            title="Dedup task", assignee_agent=self.lead_id,
            participants=[self.lead_id, self.collab_id, self.collab_id])
        self.assertEqual(roles_by_id(out2["task"]),
                         {self.lead_id: "lead",
                          self.collab_id: "collaborator"})

    def test_participant_sees_task_outsider_does_not(self):
        tid = self.make_task(participants=[self.collab_id])
        mine = [t["id"] for t in
                self.collab.list_tasks(participant=self.collab_id)["tasks"]]
        self.assertIn(tid, mine)
        unfiltered = [t["id"] for t in self.collab.list_tasks()["tasks"]]
        self.assertIn(tid, unfiltered)
        got = self.collab.get_task(tid)["task"]
        self.assertEqual(got["title"], "Shared task")
        # The outsider: not in its list, and a direct read is refused.
        outsider_list = [t["id"] for t in self.outsider.list_tasks()["tasks"]]
        self.assertNotIn(tid, outsider_list)
        with self.assertRaises(MeshError) as ctx:
            self.outsider.get_task(tid)
        self.assertEqual(ctx.exception.status, 403)
        # Admin still sees everything.
        status, out = raw_admin(self.base, "GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertIn(tid, [t["id"] for t in out["tasks"]])


class TestParticipantManagement(CollabFixture):
    def test_add_and_remove_rules(self):
        tid = self.make_task()
        out = self.req.add_participant(tid, self.collab_id)
        self.assertEqual(roles_by_id(out["task"])[self.collab_id],
                         "collaborator")
        # Only the requester (or admin) manages the roster.
        with self.assertRaises(MeshError) as ctx:
            self.lead.add_participant(tid, self.collab2_id)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(MeshError) as ctx:
            self.outsider.add_participant(tid, self.collab2_id)
        self.assertEqual(ctx.exception.status, 403)
        status, out = raw_admin(
            self.base, "POST", f"/tasks/{tid}/participants",
            {"agent_id": self.collab2_id})
        self.assertEqual(status, 200)
        self.assertIn(self.collab2_id, roles_by_id(out["task"]))
        # Removal works and immediately cuts visibility.
        out = self.req.remove_participant(tid, self.collab_id)
        self.assertNotIn(self.collab_id, roles_by_id(out["task"]))
        with self.assertRaises(MeshError) as ctx:
            self.collab.get_task(tid)
        self.assertEqual(ctx.exception.status, 403)
        # The lead cannot be removed; a non-participant cannot be removed.
        with self.assertRaises(MeshError) as ctx:
            self.req.remove_participant(tid, self.lead_id)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(MeshError) as ctx:
            self.req.remove_participant(tid, self.collab_id)
        self.assertEqual(ctx.exception.status, 404)

    def test_no_roster_changes_on_terminal_task(self):
        tid = self.make_task(participants=[self.collab_id])
        self.walk_to_done(tid)
        with self.assertRaises(MeshError) as ctx:
            self.req.add_participant(tid, self.collab2_id)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(MeshError) as ctx:
            self.req.remove_participant(tid, self.collab_id)
        self.assertEqual(ctx.exception.status, 400)

    def test_participants_must_be_orderable(self):
        # A worker may not task another worker, not even as a
        # collaborator on its own self-assigned task.
        with self.assertRaises(MeshError) as ctx:
            self.lead.create_task(
                title="worker pulling in a peer",
                assignee_agent=self.lead_id,
                participants=[self.collab_id])
        self.assertEqual(ctx.exception.status, 403)
        # An orchestrator may not add an owner-role agent.
        with self.assertRaises(MeshError) as ctx:
            self.req.create_task(
                title="orch pulling in the boss",
                assignee_agent=self.lead_id,
                participants=[self.boss_id])
        self.assertEqual(ctx.exception.status, 403)


class TestComments(CollabFixture):
    def test_thread_permissions_and_order(self):
        tid = self.make_task(participants=[self.collab_id])
        self.collab.add_comment(tid, "collab checking in")
        self.lead.add_comment(tid, "lead here, starting now")
        self.req.add_comment(tid, "requester: thanks both")
        task = self.collab.get_task(tid)["task"]
        bodies = [c["body"] for c in task["comments"]]
        self.assertEqual(bodies, ["collab checking in",
                                  "lead here, starting now",
                                  "requester: thanks both"])
        authors = [c["author"] for c in task["comments"]]
        self.assertEqual(authors, [self.collab_id, self.lead_id,
                                   self.req_id])
        with self.assertRaises(MeshError) as ctx:
            self.outsider.add_comment(tid, "let me in")
        self.assertEqual(ctx.exception.status, 403)

    def test_scanner_blocks_credential_shaped_comment(self):
        tid = self.make_task(participants=[self.collab_id])
        with self.assertRaises(MeshError) as ctx:
            self.collab.add_comment(tid, f"here is the key: {FAKE_GHP}")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.body["rule"], "github_token")
        task = self.req.get_task(tid)["task"]
        self.assertNotIn(FAKE_GHP, json.dumps(task))

    def test_no_comments_on_terminal_task(self):
        tid = self.make_task(participants=[self.collab_id])
        self.walk_to_done(tid)
        with self.assertRaises(MeshError) as ctx:
            self.collab.add_comment(tid, "late to the thread")
        self.assertEqual(ctx.exception.status, 400)


class TestTransitionRules(CollabFixture):
    def test_collaborator_cannot_transition_but_can_progress(self):
        tid = self.make_task(participants=[self.collab_id])
        with self.assertRaises(MeshError) as ctx:
            self.collab.transition(tid, "ACCEPTED")
        self.assertEqual(ctx.exception.status, 403)
        out = self.collab.progress(tid, "collab progress note")
        self.assertTrue(any(n["note"] == "collab progress note"
                            for n in out["task"]["progress_notes"]))
        # An uninvolved agent can neither transition nor add progress.
        with self.assertRaises(MeshError) as ctx:
            self.outsider.transition(tid, "ACCEPTED")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(MeshError) as ctx:
            self.outsider.progress(tid, "drive-by note")
        self.assertEqual(ctx.exception.status, 403)
        # The lead drives; the requester closes.
        self.assertEqual(
            self.lead.transition(tid, "ACCEPTED")["task"]["status"],
            "ACCEPTED")


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------

def subprocess_env(base, token):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENT_MESH_NODE"] = base
    env["AGENT_MESH_TOKEN"] = token
    return env


class McpSession:
    """A running agent_mesh.mcp_server subprocess (Phase 2 pattern)."""

    def __init__(self, base, token):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_mesh.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=subprocess_env(base, token), cwd=REPO)
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self._queue.put(line)

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        while True:
            try:
                line = self._queue.get(timeout=20)
            except queue.Empty:
                raise AssertionError(f"timeout waiting for {method}")
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    def handshake(self):
        self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "phase9-test", "version": "0"}})
        self._send({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})

    def call(self, tool, args=None):
        reply = self.request("tools/call",
                             {"name": tool, "arguments": args or {}})
        result = reply["result"]
        return json.loads(result["content"][0]["text"]), \
            result.get("isError", False)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)


class TestMcpCollaboration(CollabFixture):
    def open_session(self, token):
        session = McpSession(self.base, token)
        session.handshake()
        self.addCleanup(session.close)
        return session

    def test_mcp_create_with_participants_and_comment(self):
        req_mcp = self.open_session(self.tokens[self.req_id])
        created, is_error = req_mcp.call("mesh_task_create", {
            "title": "MCP team task", "instructions": "via tools",
            "assignee_agent": self.lead_id,
            "participants": [self.collab_id]})
        self.assertFalse(is_error)
        tid = created["task"]["id"]
        self.assertEqual(created["task"]["status"], "SENT")
        self.assertEqual(roles_by_id(created["task"]),
                         {self.lead_id: "lead",
                          self.collab_id: "collaborator"})
        collab_mcp = self.open_session(self.tokens[self.collab_id])
        out, is_error = collab_mcp.call("mesh_task_comment", {
            "task_id": tid, "text": "comment via MCP"})
        self.assertFalse(is_error)
        got, is_error = collab_mcp.call("mesh_task_get", {"task_id": tid})
        self.assertFalse(is_error)
        bodies = [c["body"] for c in got["task"]["comments"]]
        self.assertIn("comment via MCP", bodies)
        inbox, is_error = collab_mcp.call("mesh_inbox")
        self.assertFalse(is_error)
        self.assertIn(tid, [t["id"] for t in inbox["tasks_participating"]])


# ---------------------------------------------------------------------------
# Federation: lead on node A, collaborator on node B
# ---------------------------------------------------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_node_process(port, data_dir, name, admin):
    env = dict(os.environ)
    env.update({
        "AGENT_MESH_ADMIN": admin,
        "AGENT_MESH_MASTER": "test-master-passphrase-p9",
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


ADMIN_A = "test-admin-token-p9-a"
ADMIN_B = "test-admin-token-p9-b"


class TestFederatedCollaboration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir_a = tempfile.mkdtemp(prefix="agent-mesh-p9a-")
        cls.dir_b = tempfile.mkdtemp(prefix="agent-mesh-p9b-")
        cls.proc_a, cls.base_a = start_node_process(
            free_port(), cls.dir_a, "Node A", ADMIN_A)
        cls.proc_b, cls.base_b = start_node_process(
            free_port(), cls.dir_b, "Node B", ADMIN_B)
        cls.admin_a = MeshClient(cls.base_a, admin_token=ADMIN_A)
        cls.admin_b = MeshClient(cls.base_b, admin_token=ADMIN_B)
        reg = cls.admin_a.register(name="A Requester",
                                   role="orchestrator")
        cls.a_req_id = reg["agent"]["id"]
        cls.a_req = MeshClient(cls.base_a, token=reg["token"])
        reg = cls.admin_a.register(name="A Lead", role="worker")
        cls.a_lead_id = reg["agent"]["id"]
        cls.a_lead = MeshClient(cls.base_a, token=reg["token"])
        reg = cls.admin_b.register(name="B Collab", role="worker")
        cls.b_collab_id = reg["agent"]["id"]
        cls.b_collab = MeshClient(cls.base_b, token=reg["token"])
        code = cls.admin_a.pairing_code()["code"]
        cls.admin_b.pair_with(cls.base_a, code)
        wait_for(lambda: any(
            a["id"] == cls.b_collab_id and a.get("remote")
            for a in cls.admin_a.list_agents()["agents"]),
            desc="B Collab in A's directory")
        wait_for(lambda: any(
            a["id"] == cls.a_lead_id and a.get("remote")
            for a in cls.admin_b.list_agents()["agents"]),
            desc="A Lead in B's directory")

    @classmethod
    def tearDownClass(cls):
        stop_node(cls.proc_a)
        stop_node(cls.proc_b)
        shutil.rmtree(cls.dir_a, ignore_errors=True)
        shutil.rmtree(cls.dir_b, ignore_errors=True)

    def test_cross_node_collaborator_round_trip(self):
        out = self.a_req.create_task(
            title="Federated team task",
            instructions="lead on A, collaborator on B",
            assignee_agent=self.a_lead_id,
            participants=[self.b_collab_id])
        origin_id = out["task"]["id"]
        self.a_req.transition(origin_id, "SENT")

        def b_copy():
            tasks = self.b_collab.list_tasks(
                participant=self.b_collab_id)["tasks"]
            mine = [t for t in tasks
                    if t["title"] == "Federated team task"]
            return mine[0] if mine else None

        wait_for(lambda: b_copy() is not None,
                 desc="task visible to the collaborator on node B")
        copy_task = b_copy()
        self.assertEqual(copy_task["origin_node"],
                         self.admin_a.node_info()["node_id"])
        self.assertEqual(roles_by_id(copy_task),
                         {self.a_lead_id: "lead",
                          self.b_collab_id: "collaborator"})

        # The collaborator comments from node B; it lands on node A.
        self.b_collab.add_comment(copy_task["id"],
                                  "B collaborator reporting in")

        def on_a():
            t = self.a_req.get_task(origin_id)["task"]
            return any(c["body"] == "B collaborator reporting in"
                       and c["author"] == self.b_collab_id
                       for c in t["comments"])
        wait_for(on_a, desc="B's comment merged into A's origin task")

        # The lead comments on A; it lands on B's copy (read via the
        # single-task view — list views carry no comment thread).
        self.a_lead.add_comment(origin_id, "A lead acknowledging")

        def on_b_full():
            t = self.b_collab.get_task(copy_task["id"])["task"]
            bodies = [c["body"] for c in t["comments"]]
            return "A lead acknowledging" in bodies and \
                "B collaborator reporting in" in bodies
        wait_for(on_b_full, desc="A's comment merged into B's copy")

        # The remote collaborator still may not drive the lifecycle.
        with self.assertRaises(MeshError) as ctx:
            self.b_collab.transition(copy_task["id"], "ACCEPTED")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
