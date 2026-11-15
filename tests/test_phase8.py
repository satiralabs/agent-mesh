"""Phase 8 tests — v2.1.1 audit fixes. Runner:
python3 -m unittest discover -s tests -v (stdlib unittest.)

Covers the three OPEN findings from SECURITY-PERF-REPORT.md:
- F6: directory updates are pushed to paired peers when an agent
  registers / its pack rotates / it is disconnected — no manual sync.
- F3: pairing codes are >=64-bit and burn after 5 failed attempts
  from the same source.
- F2: secret scanner normalization (zero-width strip, separator-
  tolerant token shapes, spaced keywords, short password values)
  plus a decode-and-rescan pass for base64 blobs.
"""
import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_mesh.client import MeshClient, MeshError  # noqa: E402
from agent_mesh.peers import MAX_PAIR_FAILURES, PeerStore  # noqa: E402
from agent_mesh.scan import scan_text  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_A = "test-admin-token-p8-a"
ADMIN_B = "test-admin-token-p8-b"


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
        "AGENT_MESH_MASTER": "test-master-passphrase-p8",
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


# ----------------------------------------------------------------------
# F6 — directory push on register / pack rotation / disconnect
# ----------------------------------------------------------------------
class DirectoryPushFixture(unittest.TestCase):
    """Nodes A and B paired BEFORE the agents under test register."""

    @classmethod
    def setUpClass(cls):
        cls.dir_a = tempfile.mkdtemp(prefix="agent-mesh-p8a-")
        cls.dir_b = tempfile.mkdtemp(prefix="agent-mesh-p8b-")
        cls.proc_a, cls.base_a = start_node(free_port(), cls.dir_a,
                                            "Node A", ADMIN_A)
        cls.proc_b, cls.base_b = start_node(free_port(), cls.dir_b,
                                            "Node B", ADMIN_B)
        cls.admin_a = MeshClient(cls.base_a, admin_token=ADMIN_A)
        cls.admin_b = MeshClient(cls.base_b, admin_token=ADMIN_B)
        cls.node_a = cls.admin_a.node_info()["node_id"]
        cls.node_b = cls.admin_b.node_info()["node_id"]
        reg_a = cls.admin_a.register(name="Agent A1",
                                     capabilities=["coding"],
                                     role="orchestrator")
        reg_b = cls.admin_b.register(name="Agent B1",
                                     capabilities=["research"],
                                     role="orchestrator")
        cls.a1_id, cls.b1_id = reg_a["agent"]["id"], reg_b["agent"]["id"]
        cls.b1 = MeshClient(cls.base_b, token=reg_b["token"])
        code = cls.admin_a.pairing_code()["code"]
        cls.admin_b.pair_with(cls.base_a, code)
        wait_for(lambda: any(
            a["id"] == cls.b1_id and a.get("remote")
            for a in cls.admin_a.list_agents()["agents"]),
            desc="B1 in A's directory after pairing")
        wait_for(lambda: any(
            a["id"] == cls.a1_id and a.get("remote")
            for a in cls.admin_b.list_agents()["agents"]),
            desc="A1 in B's directory after pairing")

    @classmethod
    def tearDownClass(cls):
        stop_node(cls.proc_a)
        stop_node(cls.proc_b)
        shutil.rmtree(cls.dir_a, ignore_errors=True)
        shutil.rmtree(cls.dir_b, ignore_errors=True)

    def remote_copy_on_b(self, agent_id):
        for a in self.admin_b.list_agents()["agents"]:
            if a["id"] == agent_id and a.get("remote"):
                return a
        return None


class TestDirectoryPushF6(DirectoryPushFixture):
    def test_register_after_pairing_is_pushed_and_reachable(self):
        # Register on A AFTER pairing. The test never calls peer_sync
        # or peers_flush: A's register handler must enqueue the new
        # directory itself and the outbox delivers it.
        reg = self.admin_a.register(name="Late Joiner", role="worker")
        late_id = reg["agent"]["id"]
        late = MeshClient(self.base_a, token=reg["token"])
        wait_for(lambda: self.remote_copy_on_b(late_id) is not None,
                 desc="late-registered agent appears in B's directory "
                      "without any manual sync")
        # And B can actually deliver to it (before the fix the send
        # returned 201 but was stored only locally on B, forever).
        self.b1.send_message(late_id, "INFO", "welcome, late joiner")
        wait_for(lambda: any(
            m["payload"] == "welcome, late joiner"
            and m["sender"] == self.b1_id
            for m in late.list_messages(recipient=late_id)["messages"]),
            desc="message from B delivered to the late joiner on A")

    def test_disconnect_and_pack_rotation_are_pushed(self):
        reg = self.admin_a.register(name="Revolving Door", role="worker")
        agent_id = reg["agent"]["id"]
        wait_for(lambda: self.remote_copy_on_b(agent_id) is not None,
                 desc="agent visible on B after register push")
        self.admin_a.disconnect_agent(agent_id)
        wait_for(lambda: (self.remote_copy_on_b(agent_id) or {})
                 .get("status") == "revoked",
                 desc="B sees the agent revoked after disconnect push")
        # Reconnect = new pack (rotates the token, clears revoked).
        self.admin_a.agent_pack(agent_id)
        wait_for(lambda: (self.remote_copy_on_b(agent_id) or {})
                 .get("status") == "online",
                 desc="B sees the agent back online after rotation push")


# ----------------------------------------------------------------------
# F3 — pairing codes >=64-bit + burn after 5 failed attempts
# ----------------------------------------------------------------------
class TestPairingCodesF3(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agent-mesh-p8-peers-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = PeerStore(os.path.join(self.dir, "peers.db"))

    def test_codes_are_at_least_64_bit_hex(self):
        code, _ = self.store.create_code()
        self.assertGreaterEqual(len(code), 16)  # 16 hex chars = 64 bits
        self.assertTrue(all(c in "0123456789ABCDEF" for c in code))
        other, _ = self.store.create_code()
        self.assertNotEqual(code, other)

    def test_consume_once_then_reuse_and_expired_refused(self):
        code, _ = self.store.create_code()
        self.assertTrue(self.store.consume_code(code, source="owner"))
        self.assertFalse(self.store.consume_code(code, source="owner"))
        stale, _ = self.store.create_code(ttl_sec=-1)
        self.assertFalse(self.store.consume_code(stale, source="owner"))

    def test_failures_below_threshold_do_not_burn(self):
        code, _ = self.store.create_code()
        for _ in range(MAX_PAIR_FAILURES - 1):
            self.assertFalse(self.store.consume_code(
                "WRONGWRONGWRONG1", source="attacker"))
        # 4 failures from someone else: the owner's first try still works.
        self.assertTrue(self.store.consume_code(code, source="owner"))

    def test_five_failures_burn_live_codes_then_fresh_code_works(self):
        code, _ = self.store.create_code()
        for _ in range(MAX_PAIR_FAILURES):
            self.assertFalse(self.store.consume_code(
                "WRONGWRONGWRONG1", source="attacker"))
        # Burned: even the correct code, from a legitimate source, is dead.
        self.assertFalse(self.store.consume_code(code, source="owner"))
        # A fresh code pairs first-try (the burn is not a lockout).
        fresh, _ = self.store.create_code()
        self.assertTrue(self.store.consume_code(fresh, source="owner"))


class TestPairingBurnOverHttp(unittest.TestCase):
    def test_wrong_codes_burn_then_fresh_code_pairs(self):
        dir_e = tempfile.mkdtemp(prefix="agent-mesh-p8e-")
        dir_f = tempfile.mkdtemp(prefix="agent-mesh-p8f-")
        proc_e = proc_f = None
        try:
            proc_e, base_e = start_node(free_port(), dir_e, "Node E",
                                        ADMIN_A)
            proc_f, base_f = start_node(free_port(), dir_f, "Node F",
                                        ADMIN_B)
            admin_e = MeshClient(base_e, admin_token=ADMIN_A)
            admin_f = MeshClient(base_f, admin_token=ADMIN_B)
            node_e = admin_e.node_info()["node_id"]
            code = admin_e.pairing_code()["code"]
            self.assertGreaterEqual(len(code), 16)
            for _ in range(MAX_PAIR_FAILURES):
                with self.assertRaises(MeshError):
                    admin_f.pair_with(base_e, "0000000000000000")
            with self.assertRaises(MeshError):  # burned by the failures
                admin_f.pair_with(base_e, code)
            fresh = admin_e.pairing_code()["code"]
            out = admin_f.pair_with(base_e, fresh)  # first-try succeeds
            self.assertEqual(out["peer"]["node_id"], node_e)
        finally:
            if proc_e:
                stop_node(proc_e)
            if proc_f:
                stop_node(proc_f)
            shutil.rmtree(dir_e, ignore_errors=True)
            shutil.rmtree(dir_f, ignore_errors=True)


# ----------------------------------------------------------------------
# F2 — scanner normalization + base64 decode-and-rescan
# ----------------------------------------------------------------------
FAKE_GHP8 = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


class TestScannerHardeningF2(unittest.TestCase):
    """The eight obfuscations that passed the v2.1.0 scanner
    (SECURITY-PERF-REPORT §1.3) must now be caught."""

    def test_space_split_token_caught(self):
        self.assertIn("github_token", scan_text(
            "key ghp_A1b2C3d4E5f6 G7h8I9j0K1l2M3n4O5p6 end"))

    def test_newline_split_token_caught(self):
        self.assertIn("openai_key", scan_text(
            "use sk-proj-Ab3dEf5Gh7Jk\n9Mn1Pq3Rs5Tu7Vw9Xy ok"))

    def test_base64_encoded_token_caught(self):
        blob = base64.b64encode(FAKE_GHP8.encode()).decode()
        self.assertIn("github_token", scan_text(f"blob: {blob}"))
        wrapped = "\n".join(blob[i:i + 16]
                            for i in range(0, len(blob), 16))
        self.assertIn("github_token", scan_text(f"blob:\n{wrapped}"))
        creds = base64.b64encode(b"password = Sup3rSecretValue").decode()
        self.assertIn("secret_assignment", scan_text(f"cfg {creds}"))

    def test_spaced_api_key_keyword_caught(self):
        self.assertIn("secret_assignment",
                      scan_text("api key: Sup3rSecretValue99"))

    def test_lowercase_aws_key_caught(self):
        self.assertIn("aws_access_key",
                      scan_text("id akiaiosfodnn7example end"))
        self.assertIn("aws_access_key",
                      scan_text("AKIA IOSFODNN7EXAMPLE"))

    def test_short_password_value_caught(self):
        self.assertIn("secret_assignment", scan_text("password = abc123"))
        self.assertIn("secret_assignment",
                      scan_text("password = Tr0ub4dor&3xyz"))

    def test_zero_width_characters_do_not_hide_shapes(self):
        self.assertIn("secret_assignment",
                      scan_text("pass\u200bword = Sup3rSecretValue"))
        self.assertIn("github_token", scan_text(
            "ghp\u200b_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"))

    def test_dot_inserted_sk_key_caught(self):
        self.assertIn("openai_key", scan_text(
            "sk-proj.abcd1234.efgh5678.ijkl9012"))

    def test_split_jwt_caught(self):
        self.assertIn("jwt", scan_text(
            "eyJhbGciOiJIUzI1NiJ9. eyJzdWIiOiIxMjM0NTY3ODkwIn0. "
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"))

    def test_prose_and_code_still_pass(self):
        clean = [
            "let us discuss the token budget and the secret santa plan",
            "the token economy and secret plans",
            "password = changeme-now",          # placeholder
            "password = true",                  # config boolean, not a secret
            "the password = the one you chose at signup",
            "the password must be at least 8 characters long",
            "Akia said the deployment failed and went home",
            "api key: the one stored in the vault, rotate monthly",
            "fixed in commit "
            "da39a3ee5e6b4b0d3255bfef95601890afd80709 today",
            "the Authorization header uses the Bearer scheme with "
            "short tokens",
            "we use sk-learn and task-oriented design patterns",
            "ghp_ tokens are used for authentication purposes",
            "your password protects the vault and the policy is strict",
            "id <redacted> end",
        ]
        for text in clean:
            self.assertEqual(scan_text(text), [], text)


if __name__ == "__main__":
    unittest.main()
