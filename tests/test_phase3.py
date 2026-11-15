"""Phase 3 tests. Runner: python3 -m unittest discover -s tests -v
(stdlib unittest, same as Phases 1-2.)

Covers: the GUI is served by the node (root + static assets + v1 page
kept), the approvals queue end-to-end (request -> deny -> request ->
approve -> exactly one allowed use -> gated again), the activity
endpoint, the vault admin endpoints the GUI uses, and the
connection-pack endpoint. A dummy secret is byte-scanned out of every
response it could leak into.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from agent_mesh.client import MeshClient, MeshError
from agent_mesh.node import Node, serve

ADMIN = "test-admin-token-p3"
MASTER = "test-master-passphrase-p3"
P3_SECRET = "PHASE3-DUMMY-SECRET-7f2c1a-do-not-leak"


def start_node():
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p3-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER)
    server = serve(node, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, data_dir, f"http://127.0.0.1:{port}"


def raw_get(base, path, admin=False):
    req = urllib.request.Request(base + path, method="GET")
    if admin:
        req.add_header("X-Admin-Token", ADMIN)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers.get_content_type(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


class Phase3Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        reg = cls.admin.register(name="Phase3 Agent", capabilities=["coding"],
                                 role="worker")
        cls.a_id = reg["agent"]["id"]
        cls.a_token = reg["token"]
        cls.a = MeshClient(cls.base, token=cls.a_token)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)


class TestGuiServed(Phase3Fixture):
    def test_root_serves_the_gui(self):
        status, ctype, body = raw_get(self.base, "/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html")
        self.assertIn(b'id="screen-dashboard"', body)
        self.assertIn(b'id="screen-vault"', body)
        self.assertIn(b'id="screen-approvals"', body)
        self.assertIn(b'id="login-screen"', body)

    def test_static_assets_served(self):
        status, ctype, body = raw_get(self.base, "/ui/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"agentMeshAdmin", body)  # sessionStorage token key
        self.assertNotIn(b"localStorage.setItem", body)
        status, ctype, body = raw_get(self.base, "/ui/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--bg-dark", body)

    def test_v1_page_kept(self):
        status, ctype, body = raw_get(self.base, "/v1")
        self.assertEqual(status, 200)
        self.assertIn(b"Agent Mesh", body)


class TestApprovalFlow(Phase3Fixture):
    def test_approve_deny_and_one_time_consumption(self):
        self.admin.vault_add_service(
            service="gatedsvc", secret=P3_SECRET,
            upstream="https://example.invalid/gated",
            acl={self.a_id: ["spend"]}, requires_approval=True)
        seen = []

        first = self.a.vault_use_check("gatedsvc", "spend")
        seen.append(json.dumps(first))
        self.assertEqual(first["outcome"], "needs_approval")
        first_id = first["approval_id"]

        pending = self.admin.approvals_list("pending")["approvals"]
        self.assertIn(first_id, [a["id"] for a in pending])

        # Deny: no use is granted, and nothing is consumed.
        denied = self.admin.approval_decide(first_id, False)
        self.assertEqual(denied["approval"]["status"], "denied")
        second = self.a.vault_use_check("gatedsvc", "spend")
        seen.append(json.dumps(second))
        self.assertNotEqual(second["outcome"], "allow")
        self.assertEqual(second["outcome"], "needs_approval")

        # Approve the fresh request: exactly one use is allowed.
        second_id = second["approval_id"]
        self.assertNotEqual(second_id, first_id)
        approved = self.admin.approval_decide(second_id, True)
        seen.append(json.dumps(approved))
        self.assertEqual(approved["approval"]["status"], "approved")

        third = self.a.vault_use_check("gatedsvc", "spend")
        seen.append(json.dumps(third))
        self.assertEqual(third["outcome"], "allow")

        fourth = self.a.vault_use_check("gatedsvc", "spend")
        seen.append(json.dumps(fourth))
        self.assertEqual(fourth["outcome"], "needs_approval")

        self.assertNotIn(P3_SECRET, "".join(seen))

    def test_approvals_list_is_admin_only(self):
        status, _ctype, _body = raw_get(self.base, "/approvals")
        self.assertEqual(status, 401)

    def test_activity_has_approval_events_and_no_secret(self):
        self.admin.vault_add_service(
            service="actsvc", secret=P3_SECRET,
            upstream="https://example.invalid/act",
            acl={self.a_id: ["read"]}, requires_approval=True)
        req = self.a.vault_use_check("actsvc", "read")
        self.admin.approval_decide(req["approval_id"], True)
        out = self.admin.activity()
        blob = json.dumps(out)
        self.assertIn("approval_requested", blob)
        self.assertIn("approval_approved", blob)
        self.assertNotIn(P3_SECRET, blob)


class TestVaultGuiEndpoints(Phase3Fixture):
    def test_add_list_update_delete(self):
        self.admin.vault_add_service(
            service="guisvc", secret=P3_SECRET,
            upstream="https://example.invalid/gui", acl={})
        services = self.admin.vault_services()["services"]
        self.assertIn("guisvc", services)
        self.assertTrue(services["guisvc"]["has_secret"])
        self.assertNotIn(P3_SECRET, json.dumps(services))

        self.admin.vault_update_service(
            "guisvc", acl={self.a_id: ["read"]}, requires_approval=False)
        services = self.admin.vault_services()["services"]
        self.assertEqual(services["guisvc"]["acl"], {self.a_id: ["read"]})

        # The ACL the GUI just set is live for the broker check.
        out = self.a.vault_use_check("guisvc", "read")
        self.assertEqual(out["outcome"], "allow")

        self.admin.vault_delete_service("guisvc")
        services = self.admin.vault_services()["services"]
        self.assertNotIn("guisvc", services)


class TestPackEndpoint(Phase3Fixture):
    def test_pack_via_api_rotates_token(self):
        reg = self.admin.register(name="Pack Target", role="worker")
        target_id, old_token = reg["agent"]["id"], reg["token"]
        out = self.admin.agent_pack(target_id)
        blob = json.dumps(out)
        self.assertIn("agent_mesh.mcp_server", blob)
        env = out["pack"]["mcpServers"]["agent-mesh"]["env"]
        self.assertEqual(env["AGENT_MESH_AGENT"], target_id)
        new_token = env["AGENT_MESH_TOKEN"]
        self.assertTrue(new_token)
        self.assertNotEqual(new_token, old_token)
        # New token works; the rotated-out one does not.
        as_packed = MeshClient(self.base, token=new_token)
        self.assertIn("agents", as_packed.list_agents())
        stale = MeshClient(self.base, token=old_token)
        with self.assertRaises(MeshError) as ctx:
            stale.list_agents()
        self.assertEqual(ctx.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
