"""Phase 7 tests — v2.1: presence fix, vault credential sharing,
handover automation, inbox watcher, universal packs, release artifacts.

Runner: python3 -m unittest discover -s tests -v (stdlib unittest).
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from agent_mesh.client import MeshClient, MeshError
from agent_mesh.node import Node, serve
from agent_mesh.peers import PeerStore
from agent_mesh.registry import Registry, presence_state

ADMIN = "test-admin-token-p7"
MASTER = "test-master-passphrase-p7"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def start_node(name="p7-node"):
    data_dir = tempfile.mkdtemp(prefix="agent-mesh-p7-")
    node = Node(data_dir, admin_token=ADMIN, master_passphrase=MASTER,
                node_name=name)
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


# ---------------------------------------------------------------------------
# C. Presence fix
# ---------------------------------------------------------------------------
class TestPresenceComputation(unittest.TestCase):
    def test_boundaries(self):
        now = 1_700_000_000.0
        self.assertEqual(presence_state(now, now=now), "online")
        self.assertEqual(presence_state(now - 119, now=now), "online")
        self.assertEqual(presence_state(now - 120, now=now), "idle")
        self.assertEqual(presence_state(now - 899, now=now), "idle")
        self.assertEqual(presence_state(now - 900, now=now), "offline")
        self.assertEqual(presence_state(now - 86400, now=now), "offline")

    def test_revoked_always_wins(self):
        now = time.time()
        self.assertEqual(presence_state(now, status="revoked", now=now),
                         "revoked")

    def test_registry_backdated_row_reports_offline(self):
        data_dir = tempfile.mkdtemp(prefix="agent-mesh-p7-reg-")
        reg = Registry(os.path.join(data_dir, "agents.db"))
        agent, _token = reg.register("Stale Agent")
        self.assertEqual(agent["presence"], "online")
        conn = sqlite3.connect(os.path.join(data_dir, "agents.db"))
        with conn:
            conn.execute("UPDATE agents SET last_seen=? WHERE id=?",
                         (time.time() - 3600, agent["id"]))
        conn.close()
        listed = {a["id"]: a for a in reg.list_agents()}
        self.assertEqual(listed[agent["id"]]["presence"], "offline")
        # The stored flag still says online — presence is what changed.
        self.assertEqual(listed[agent["id"]]["status"], "online")

    def test_remote_agents_carry_origin_last_seen(self):
        data_dir = tempfile.mkdtemp(prefix="agent-mesh-p7-peers-")
        store = PeerStore(os.path.join(data_dir, "peers.db"))
        origin_seen = time.time() - 3600  # an hour ago on the origin node
        store.set_remote_agents("nod_origin", [{
            "id": "agt_remote1", "name": "Remote One",
            "capabilities": [], "role": "worker", "status": "online",
            "machine": "elsewhere", "last_seen": origin_seen}])
        agents = store.remote_agents()
        self.assertEqual(len(agents), 1)
        # last_seen is the ORIGIN's timestamp, not the sync time...
        self.assertAlmostEqual(agents[0]["last_seen"], origin_seen, delta=1)
        # ...so presence is honestly offline, not freshly online.
        self.assertEqual(agents[0]["presence"], "offline")


# ---------------------------------------------------------------------------
# A. Vault credential sharing
# ---------------------------------------------------------------------------
LOGIN_SECRET = "login-password-9f8e7d6c5b"
SSH_SECRET = "ssh-key-material-1a2b3c4d5e"
API_SECRET = "api-secret-value-0z9y8x7w"


class TestVaultSharing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.node, cls.data_dir, cls.base = start_node()
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)
        cls.alice = cls.admin.register(name="P7 Alice", role="worker")
        cls.bob = cls.admin.register(name="P7 Bob", role="worker")
        cls.alice_c = MeshClient(cls.base, token=cls.alice["token"])
        cls.bob_c = MeshClient(cls.base, token=cls.bob["token"])
        cls.alice_id = cls.alice["agent"]["id"]
        cls.bob_id = cls.bob["agent"]["id"]
        # A login credential and an ssh credential, owned by the node.
        status, _ = raw(cls.base, "POST", "/vault/services", {
            "service": "webmail", "secret": LOGIN_SECRET,
            "type": "login",
            "meta": {"username": "susan@satiralabs.example",
                     "url": "https://mail.example.com"}}, admin=ADMIN)
        assert status == 201, status
        status, _ = raw(cls.base, "POST", "/vault/services", {
            "service": "vps-ssh", "secret": SSH_SECRET,
            "type": "ssh",
            "meta": {"host": "vps.example.com", "user": "root",
                     "port": 22}}, admin=ADMIN)
        assert status == 201, status
        status, _ = raw(cls.base, "POST", "/vault/services", {
            "service": "someapi", "secret": API_SECRET,
            "type": "api_key",
            "meta": {"label": "Some API"}}, admin=ADMIN)
        assert status == 201, status

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_type_metadata_is_listable_without_secrets(self):
        services = self.admin.vault_services()["services"]
        self.assertEqual(services["webmail"]["type"], "login")
        self.assertEqual(services["webmail"]["meta"]["username"],
                         "susan@satiralabs.example")
        self.assertEqual(services["vps-ssh"]["type"], "ssh")
        self.assertEqual(services["vps-ssh"]["meta"]["host"],
                         "vps.example.com")
        blob = json.dumps(services)
        for secret in (LOGIN_SECRET, SSH_SECRET, API_SECRET):
            self.assertNotIn(secret, blob)

    def test_use_mode_returns_connection_details_only(self):
        # Owner shares webmail + vps-ssh with Alice in `use` mode.
        self.admin.vault_grant("webmail", self.alice_id, "use")
        self.admin.vault_grant("vps-ssh", self.alice_id, "use")
        out = self.alice_c.vault_credential_use("webmail")
        self.assertEqual(out["outcome"], "allow")
        self.assertEqual(out["username"], "susan@satiralabs.example")
        self.assertEqual(out["url"], "https://mail.example.com")
        self.assertNotIn(LOGIN_SECRET, json.dumps(out))
        out = self.alice_c.vault_credential_use("vps-ssh")
        self.assertEqual(out["outcome"], "allow")
        self.assertEqual(out["host"], "vps.example.com")
        self.assertEqual(out["user"], "root")
        self.assertNotIn(SSH_SECRET, json.dumps(out))

    def test_use_denied_without_grant(self):
        with self.assertRaises(MeshError) as ctx:
            self.bob_c.vault_credential_use("webmail")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body["outcome"], "deny")

    def test_reveal_denied_without_grant(self):
        with self.assertRaises(MeshError) as ctx:
            self.bob_c.vault_reveal("vps-ssh")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body["outcome"], "deny")
        self.assertNotIn(SSH_SECRET, json.dumps(ctx.exception.body))

    def test_reveal_per_use_approval_succeeds_once_then_regates(self):
        self.admin.vault_grant("vps-ssh", self.alice_id, "reveal")
        # First attempt: gated, an approval request is created.
        out = self.alice_c.vault_reveal("vps-ssh")
        self.assertEqual(out["outcome"], "needs_approval")
        self.assertIn("approval_id", out)
        self.assertNotIn(SSH_SECRET, json.dumps(out))
        # Owner approves exactly one reveal.
        self.admin.approval_decide(out["approval_id"], True)
        out = self.alice_c.vault_reveal("vps-ssh")
        self.assertEqual(out["outcome"], "allow")
        self.assertEqual(out["secret"], SSH_SECRET)
        # The approval is consumed: the next reveal is gated again.
        out = self.alice_c.vault_reveal("vps-ssh")
        self.assertEqual(out["outcome"], "needs_approval")
        self.assertNotIn(SSH_SECRET, json.dumps(out))

    def test_standing_reveal_needs_no_approval(self):
        self.admin.vault_grant("someapi", self.bob_id, "reveal",
                               standing_reveal=True)
        out = self.bob_c.vault_reveal("someapi")
        self.assertEqual(out["outcome"], "allow")
        self.assertEqual(out["secret"], API_SECRET)
        # ...and again (standing means standing).
        out = self.bob_c.vault_reveal("someapi")
        self.assertEqual(out["outcome"], "allow")

    def test_use_grant_does_not_allow_reveal(self):
        # A `use` grant is brokered-only — it must never become a reveal.
        self.admin.vault_grant("webmail", self.alice_id, "use")
        with self.assertRaises(MeshError) as ctx:
            self.alice_c.vault_reveal("webmail")
        self.assertEqual(ctx.exception.body["outcome"], "deny")

    def test_reveal_is_audited_without_secret_bytes(self):
        # Do one approved reveal here so the audit assertions below do
        # not depend on other tests having run first.
        self.admin.vault_grant("vps-ssh", self.bob_id, "reveal",
                               standing_reveal=True)
        out = self.bob_c.vault_reveal("vps-ssh")
        self.assertEqual(out["outcome"], "allow")
        audit_path = os.path.join(self.data_dir, "audit.log")
        with open(audit_path, "rb") as fh:
            blob = fh.read()
        for secret in (LOGIN_SECRET, SSH_SECRET, API_SECRET):
            self.assertNotIn(secret.encode(), blob)
        entries = [json.loads(line) for line in blob.decode().splitlines()
                   if line.strip()]
        reveals = [e for e in entries if e.get("action") == "reveal"]
        self.assertTrue(reveals, "expected reveal audit entries")
        self.assertTrue(any(e["outcome"].startswith("allow")
                            for e in reveals))

    def test_grant_revoke_cuts_access(self):
        self.admin.vault_grant("someapi", self.alice_id, "use")
        out = self.alice_c.vault_credential_use("someapi")
        self.assertEqual(out["outcome"], "allow")
        self.assertNotIn(API_SECRET, json.dumps(out))
        self.admin.vault_revoke_grant("someapi", self.alice_id)
        with self.assertRaises(MeshError) as ctx:
            self.alice_c.vault_credential_use("someapi")
        self.assertEqual(ctx.exception.body["outcome"], "deny")

    def test_gui_vault_has_type_and_share_controls(self):
        with open(os.path.join(REPO, "agent_mesh", "web2", "index.html")) as fh:
            html = fh.read()
        with open(os.path.join(REPO, "agent_mesh", "web2", "app.js")) as fh:
            js = fh.read()
        self.assertIn('id="vault-type"', html)
        self.assertIn('id="vault-fields-login"', html)
        self.assertIn('id="vault-fields-ssh"', html)
        self.assertIn("data-share", js)
        self.assertIn("/grants", js)


# ---------------------------------------------------------------------------
# B. Handover automation
# ---------------------------------------------------------------------------
class TestHandover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.node, cls.data_dir, cls.base = start_node("p7-ho")
        cls.admin = MeshClient(cls.base, admin_token=ADMIN)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _register(self, name):
        reg = self.admin.register(name=name, role="worker")
        return reg["agent"], MeshClient(self.base, token=reg["token"])

    def test_rules_endpoint(self):
        _agent, client = self._register("P7 Rules Reader")
        out = client.handover_rules()
        self.assertTrue(out["version"])
        self.assertIn("unique", out["rules"].lower())
        self.assertIn("never", out["rules"].lower())

    def test_starter_delivered_exactly_once(self):
        agent, client = self._register("P7 Starter")
        # Registration already delivered it.
        msgs = client.list_messages()["messages"]
        starters = [m for m in msgs if m["msg_type"] == "HANDOVER_RULES"]
        self.assertEqual(len(starters), 1)
        self.assertIn("mesh_handover_log", str(starters[0]["payload"]))
        # Re-heartbeats must NOT resend it.
        client.heartbeat()
        client.heartbeat()
        msgs = client.list_messages()["messages"]
        starters = [m for m in msgs if m["msg_type"] == "HANDOVER_RULES"]
        self.assertEqual(len(starters), 1)

    def test_entry_posts_persists_and_exports(self):
        agent, client = self._register("P7 Filer")
        out = client.handover_log(
            title="Built the widget", asked="build a widget",
            done="built it", verified="ran the widget test suite",
            decisions="used stdlib only",
            files="arch-pc: /home/benji/widget/widget.py",
            open="deploy to the VPS")
        entry = out["entry"]
        self.assertEqual(entry["agent_name"], "P7 Filer")
        # Persisted: admin lists all entries, the agent lists its own.
        all_entries = self.admin.handover_entries()["entries"]
        self.assertIn(entry["id"], [e["id"] for e in all_entries])
        own = client.handover_entries()["entries"]
        self.assertEqual([e["id"] for e in own], [entry["id"]])
        # Exported markdown contains the entry under the agent's name.
        export = os.path.join(self.data_dir, "HANDOVER-p7-ho.md")
        self.assertTrue(os.path.exists(export))
        with open(export) as fh:
            text = fh.read()
        self.assertIn("## P7 Filer", text)
        self.assertIn("Built the widget", text)
        self.assertIn("arch-pc: /home/benji/widget/widget.py", text)

    def test_scanner_blocks_credential_shaped_entry(self):
        _agent, client = self._register("P7 Leaker")
        with self.assertRaises(MeshError) as ctx:
            client.handover_log(
                title="oops",
                done="the deploy password = hunter2hunter2 is set")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("secret scanner",
                      json.dumps(ctx.exception.body).lower())
        # Nothing was stored.
        entries = self.admin.handover_entries()["entries"]
        self.assertFalse(any(e["title"] == "oops" for e in entries))

    def test_cli_files_an_entry(self):
        reg = self.admin.register(name="P7 CLI Filer", role="worker")
        env = dict(os.environ)
        env["AGENT_MESH_NODE"] = self.base
        env["AGENT_MESH_TOKEN"] = reg["token"]
        env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "agent_mesh.handover",
             "--title", "CLI session", "--asked", "file via cli",
             "--done", "filed", "--verified", "this test"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        entries = self.admin.handover_entries()["entries"]
        self.assertTrue(any(e["title"] == "CLI session"
                            and e["agent_name"] == "P7 CLI Filer"
                            for e in entries))


if __name__ == "__main__":
    unittest.main()
