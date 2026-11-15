"""Phase 6 tests — admin disconnect / reconnect of an agent.

Runner: python3 -m unittest discover -s tests -v (stdlib unittest).

Covers: disconnect revokes the token (next call 401), the agent stays
listed as 'revoked', admin can still read its tasks, reconnect via a
fresh pack restores access with a NEW token while the revoked token
stays dead, and a non-admin agent cannot disconnect anyone.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from agent_mesh.client import MeshClient, MeshError
from agent_mesh.node import Node, serve

ADMIN = "test-admin-token-p6"
MASTER = "test-master-passphrase-p6"


def start_node():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p6-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, node, data_dir, f"http://127.0.0.1:{port}"


def raw(base, method, path, body=None, admin=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if admin:
        req.add_header("X-Admin-Token", admin)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class TestDisconnect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.node, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        reg = cls.admin.register(name="P6 victim", role="orchestrator")
        cls.agent = reg["agent"]
        cls.old_token = reg["token"]
        cls.victim = MeshClient(cls.base, token=cls.old_token)
        other = cls.admin.register(name="P6 bystander", role="worker")
        cls.other_token = other["token"]
        cls.other_id = other["agent"]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_full_disconnect_reconnect_flow(self):
        aid = self.agent["id"]
        # 1. The agent's token works before disconnect.
        out = self.victim.heartbeat()
        self.assertEqual(out["agent"]["id"], aid)
        # Give it a task (owner/admin actor orders it) so we can check
        # admin readback after the disconnect.
        status, task = raw(self.base, "POST", "/tasks",
                           {"title": "P6 task for victim",
                            "instructions": "stand by",
                            "assignee_agent": aid}, admin=ADMIN)
        self.assertEqual(status, 201, task)
        task_id = task["task"]["id"] if "task" in task else task["id"]

        # 2. A non-admin agent trying to disconnect someone is refused.
        status, _ = raw(self.base, "POST", f"/agents/{aid}/disconnect",
                        {}, token=self.other_token)
        self.assertIn(status, (401, 403))
        # ...and the victim is still alive after that attempt.
        self.victim.heartbeat()

        # 3. Admin disconnects.
        out = self.admin.disconnect_agent(aid)
        self.assertEqual(out["agent"]["status"], "revoked")

        # 4. The old token is dead: heartbeat and inbox both 401.
        with self.assertRaises(MeshError) as ctx:
            self.victim.heartbeat()
        self.assertEqual(ctx.exception.status, 401)
        status, _ = raw(self.base, "GET", "/messages", token=self.old_token)
        self.assertEqual(status, 401)

        # 5. The agent is still listed, as revoked.
        status, listing = raw(self.base, "GET", "/agents", admin=ADMIN)
        self.assertEqual(status, 200)
        mine = [a for a in listing["agents"] if a["id"] == aid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["status"], "revoked")

        # 6. Admin can still read the revoked agent's tasks.
        status, tasks = raw(self.base, "GET", "/tasks", admin=ADMIN)
        self.assertEqual(status, 200)
        ids = [t["id"] for t in tasks["tasks"]]
        self.assertIn(task_id, ids)

        # 7. The disconnect was audited.
        status, activity = raw(self.base, "GET", "/activity?limit=50",
                               admin=ADMIN)
        self.assertEqual(status, 200)
        events = [e.get("event") for e in activity["entries"]]
        self.assertIn("agent_disconnected", events)

        # 8. Reconnect via a fresh pack: new token works, status active.
        pack_out = self.admin.agent_pack(aid)
        new_token = pack_out["pack"]["mcpServers"]["agent-mesh"]["env"][
            "AGENT_MESH_TOKEN"]
        self.assertNotEqual(new_token, self.old_token)
        revived = MeshClient(self.base, token=new_token)
        out = revived.heartbeat()
        self.assertEqual(out["agent"]["status"], "online")
        status, listing = raw(self.base, "GET", "/agents", admin=ADMIN)
        mine = [a for a in listing["agents"] if a["id"] == aid]
        self.assertEqual(mine[0]["status"], "online")

        # 9. The revoked token stays dead even after reconnect.
        status, _ = raw(self.base, "POST", "/agents/heartbeat", {},
                        token=self.old_token)
        self.assertEqual(status, 401)

    def test_disconnect_unknown_agent_404(self):
        with self.assertRaises(MeshError) as ctx:
            self.admin.disconnect_agent("agt_doesnotexist")
        self.assertEqual(ctx.exception.status, 404)

    def test_gui_has_disconnect_controls(self):
        here = os.path.dirname(os.path.abspath(__file__))
        js_path = os.path.join(here, "..", "agent_mesh", "web2", "app.js")
        with open(js_path) as fh:
            js = fh.read()
        self.assertIn("data-disconnect", js)
        self.assertIn("disconnectAgent", js)
        self.assertIn("Reconnect (new pack)", js)
        self.assertIn("/disconnect", js)


if __name__ == "__main__":
    unittest.main()
