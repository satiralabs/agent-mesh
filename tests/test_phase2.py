"""Phase 2 tests. Runner: python3 -m unittest discover -s tests -v
(stdlib unittest, same as Phase 1.)

Covers: MCP handshake + tools/list, the full pilot flow driven entirely
through two MCP server subprocesses, mesh_use_service decisions via MCP
with a secret-leak scan, the file-drop adapter, the CLI shim, and the
connection pack generator.
"""
import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_mesh.client import MeshClient
from agent_mesh.node import Node, serve

ADMIN = "test-admin-token-p2"
MASTER = "test-master-passphrase-p2"
P2_SECRET = "PHASE2-DUMMY-SECRET-b41d90-do-not-leak"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_TOOLS = {
    "mesh_agents_list", "mesh_send", "mesh_inbox", "mesh_task_create",
    "mesh_task_update", "mesh_task_list", "mesh_task_get",
    "mesh_use_service",
    # v2.1: the tool set grew by design (handover filing).
    "mesh_handover_log",
}


def start_node():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p2-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, data_dir, f"http://127.0.0.1:{port}"


def subprocess_env(base, token=None, extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENT_MESH_NODE"] = base
    if token:
        env["AGENT_MESH_TOKEN"] = token
    if extra:
        env.update(extra)
    return env


class McpSession:
    """A running agent_mesh.mcp_server subprocess, spoken to in JSON-RPC."""

    def __init__(self, base, token):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_mesh.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=subprocess_env(base, token), cwd=REPO_ROOT)
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._next_id = 0
        self.received = []  # every raw line the server sent (leak scans)

    def _pump(self):
        for line in self.proc.stdout:
            self.received.append(line)
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
                raise AssertionError(f"timeout waiting for reply to {method}")
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def handshake(self):
        reply = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "phase2-test", "version": "0"}})
        self.notify("notifications/initialized")
        return reply["result"]

    def call(self, tool, args=None):
        """Returns (parsed_content, is_error, raw_result)."""
        reply = self.request("tools/call",
                             {"name": tool, "arguments": args or {}})
        result = reply["result"]
        text = result["content"][0]["text"]
        return json.loads(text), result.get("isError", False), result

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)
        try:
            self.proc.stdout.close()
        except Exception:
            pass


class Phase2Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        reg_a = cls.admin.register(name="Pilot A", capabilities=["coding"],
                                   role="orchestrator")
        reg_b = cls.admin.register(name="Pilot B", capabilities=["research"],
                                   role="worker")
        cls.a_id, cls.b_id = reg_a["agent"]["id"], reg_b["agent"]["id"]
        cls.a_token, cls.b_token = reg_a["token"], reg_b["token"]
        cls.a = MeshClient(cls.base, token=cls.a_token)
        cls.b = MeshClient(cls.base, token=cls.b_token)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def open_session(self, token):
        session = McpSession(self.base, token)
        session.handshake()
        self.addCleanup(session.close)
        return session


class TestMcpHandshake(Phase2Fixture):
    def test_initialize_and_tools_list(self):
        session = McpSession(self.base, self.a_token)
        self.addCleanup(session.close)
        result = session.handshake()
        self.assertIn("protocolVersion", result)
        self.assertEqual(result["serverInfo"]["name"], "agent-mesh")
        reply = session.request("tools/list")
        names = {t["name"] for t in reply["result"]["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_unknown_method_is_method_not_found(self):
        session = self.open_session(self.a_token)
        reply = session.request("bogus/method")
        self.assertEqual(reply["error"]["code"], -32601)

    def test_unknown_tool_is_error_result_not_crash(self):
        session = self.open_session(self.a_token)
        _parsed, is_error, _raw = session.call("mesh_nope", {})
        self.assertTrue(is_error)
        # Server still alive afterwards.
        parsed, is_error, _ = session.call("mesh_agents_list")
        self.assertFalse(is_error)
        self.assertIn("agents", parsed)


class TestMcpPilotFlow(Phase2Fixture):
    def test_two_agents_task_and_message_flow(self):
        a = self.open_session(self.a_token)
        b = self.open_session(self.b_token)

        created, is_error, _ = a.call("mesh_task_create", {
            "title": "MCP pilot task", "instructions": "do the thing",
            "assignee": self.b_id})
        self.assertFalse(is_error)
        task = created["task"]
        self.assertEqual(task["status"], "SENT")
        self.assertEqual(task["assignee_agent"], self.b_id)
        tid = task["id"]

        inbox, is_error, _ = b.call("mesh_inbox")
        self.assertFalse(is_error)
        self.assertIn(tid, [t["id"] for t in inbox["tasks_assigned"]])

        for action, expected in (("accept", "ACCEPTED"),
                                 ("start", "IN_PROGRESS"),
                                 ("review", "IN_REVIEW")):
            kw = {"task_id": tid, "action": action}
            if action == "start":
                kw["note"] = "working on it"
            out, is_error, _ = b.call("mesh_task_update", kw)
            self.assertFalse(is_error, out)
            self.assertEqual(out["task"]["status"], expected)

        done, is_error, _ = a.call("mesh_task_update", {
            "task_id": tid, "action": "done", "result": "pilot result ok"})
        self.assertFalse(is_error)
        self.assertEqual(done["task"]["status"], "DONE")

        got, is_error, _ = a.call("mesh_task_get", {"task_id": tid})
        self.assertFalse(is_error)
        self.assertEqual(got["task"]["result"], "pilot result ok")
        notes = [n["note"] for n in got["task"]["progress_notes"]]
        self.assertIn("working on it", notes)

        _sent, is_error, _ = a.call("mesh_send", {
            "to": self.b_id, "type": "INFO", "text": "hello via mcp"})
        self.assertFalse(is_error)
        inbox, is_error, _ = b.call("mesh_inbox")
        payloads = [m["payload"] for m in inbox["messages"]]
        self.assertIn("hello via mcp", payloads)

    def test_use_service_decisions_and_no_secret_leak(self):
        self.admin.vault_add_service(
            service="p2svc", secret=P2_SECRET,
            upstream="https://example.invalid/p2",
            acl={self.a_id: ["read"]})
        a = self.open_session(self.a_token)
        b = self.open_session(self.b_token)

        allowed, is_error, _ = a.call("mesh_use_service", {
            "service": "p2svc", "action": "read"})
        self.assertFalse(is_error)
        self.assertEqual(allowed["outcome"], "allow")

        denied, is_error, _ = b.call("mesh_use_service", {
            "service": "p2svc", "action": "read"})
        self.assertFalse(is_error)
        self.assertEqual(denied["outcome"], "deny")

        everything = "".join(a.received + b.received)
        self.assertNotIn(P2_SECRET, everything)


class TestFileDrop(Phase2Fixture):
    def _run_filedrop(self, drop_dir):
        return subprocess.run(
            [sys.executable, "-m", "agent_mesh.filedrop",
             "--dir", drop_dir, "--once"],
            capture_output=True, text=True,
            env=subprocess_env(self.base, self.a_token), cwd=REPO_ROOT,
            timeout=30)

    def test_task_drop_processed_and_bad_file_failed(self):
        drop = tempfile.mkdtemp(prefix="mesh-drop-")
        self.addCleanup(shutil.rmtree, drop, True)
        with open(os.path.join(drop, "order1.json"), "w") as fh:
            json.dump({"kind": "task", "title": "Dropped task",
                       "instructions": "from a file",
                       "assignee_agent": self.b_id}, fh)
        run = self._run_filedrop(drop)
        self.assertEqual(run.returncode, 0, run.stderr)
        titles = [t["title"] for t in self.a.list_tasks()["tasks"]]
        self.assertIn("Dropped task", titles)
        self.assertTrue(os.path.exists(
            os.path.join(drop, "processed", "order1.json")))

        with open(os.path.join(drop, "bad.json"), "w") as fh:
            fh.write("{ this is not json")
        run = self._run_filedrop(drop)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(os.path.exists(
            os.path.join(drop, "failed", "bad.json")))
        self.assertTrue(os.path.exists(
            os.path.join(drop, "failed", "bad.json.error.txt")))


class TestCliShim(Phase2Fixture):
    def _run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "agent_mesh.mesh_cli", *argv],
            capture_output=True, text=True,
            env=subprocess_env(self.base, self.a_token), cwd=REPO_ROOT,
            timeout=30)

    def test_task_create_and_list(self):
        run = self._run_cli("task-create", "--title", "CLI shim task",
                            "--assignee", self.b_id)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("CLI shim task", run.stdout)
        run = self._run_cli("task-list")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("CLI shim task", run.stdout)


class TestPackGenerator(Phase2Fixture):
    def test_pack_file_is_0600_and_usable(self):
        out = os.path.join(tempfile.mkdtemp(prefix="mesh-pack-"), "pack.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        run = subprocess.run(
            [sys.executable, "-m", "agent_mesh.pack",
             "--node", self.base, "--name", "Pack Test Agent",
             "--capabilities", "coding,research", "--out", out],
            capture_output=True, text=True,
            env=subprocess_env(self.base,
                               extra={"AGENT_MESH_ADMIN": ADMIN}),
            cwd=REPO_ROOT, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        mode = stat.S_IMODE(os.stat(out).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        with open(out) as fh:
            pack = json.load(fh)
        entry = pack["mcpServers"]["agent-mesh"]
        self.assertIn("agent_mesh.mcp_server", entry["args"])
        self.assertEqual(entry["env"]["AGENT_MESH_NODE"], self.base)
        token = entry["env"]["AGENT_MESH_TOKEN"]
        self.assertTrue(token)
        # The packed token really authenticates against the node.
        as_packed = MeshClient(self.base, token=token)
        names = [x["name"] for x in as_packed.list_agents()["agents"]]
        self.assertIn("Pack Test Agent", names)


if __name__ == "__main__":
    unittest.main()
