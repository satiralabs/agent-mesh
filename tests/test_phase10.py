"""Phase 10 tests — v2.2.2 participant-removal tombstones. Runner:
python3 -m unittest discover -s tests -v (stdlib unittest.)

v2.2 merged task rosters as monotonic unions, so a removed
collaborator kept read access to the peer copy until the task ended.
v2.2.2 records every removal as a tombstone {agent_id, removed_at,
removed_by} that travels inside the task snapshot. These tests pin
the merge rule at the store level (removal beats an older add, a
newer re-add revives, tombstones union keeping the latest, a
tombstone blocks a stale add for an agent the node never saw) and
then prove the whole path live: two node processes, a collaborator
on B removed on A loses the task on B (403), and a re-add restores
it.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_mesh.client import MeshClient, MeshError  # noqa: E402
from agent_mesh.tasks import TaskStore  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def roster(task):
    return {p["agent_id"]: p["role"] for p in task["participants"]}


def removals_by_id(task):
    return {r["agent_id"]: r for r in task["participant_removals"]}


def make_snap(participants=(), removals=(), status="SENT"):
    return {"id": "tsk_origin_p10", "title": "Federated task",
            "instructions": "", "requester": "req",
            "assignee_agent": "lead", "assignee_capability": None,
            "status": status, "priority": "normal",
            "progress_notes": [], "result": None,
            "created_at": 900.0, "updated_at": 1000.0,
            "participants": list(participants),
            "participant_removals": list(removals), "comments": []}


def add(agent_id, added_at, role="collaborator"):
    return {"agent_id": agent_id, "role": role, "added_by": "req",
            "added_at": added_at}


def tomb(agent_id, removed_at, removed_by="req"):
    return {"agent_id": agent_id, "removed_at": removed_at,
            "removed_by": removed_by}


class TestRemovalMergeRule(unittest.TestCase):
    """Store-level: crafted snapshots with fixed timestamps merged
    through upsert_federated (the same entry point /peer/task_sync
    uses), so the rule is tested exactly as federation applies it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agent-mesh-p10-")
        self.store = TaskStore(os.path.join(self.dir, "tasks.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def merge(self, snap):
        return self.store.upsert_federated(snap, "node-a")

    def test_removal_wins_over_older_add(self):
        task = self.merge(make_snap(
            participants=[add("lead", 900.0, "lead"),
                          add("collab", 1000.0)]))
        self.assertEqual(roster(task),
                         {"lead": "lead", "collab": "collaborator"})
        task = self.merge(make_snap(
            participants=[add("collab", 1000.0)],
            removals=[tomb("collab", 2000.0)]))
        self.assertEqual(roster(task), {"lead": "lead"})
        self.assertEqual(
            removals_by_id(task)["collab"]["removed_at"], 2000.0)
        self.assertEqual(
            removals_by_id(task)["collab"]["removed_by"], "req")

    def test_stale_snapshot_does_not_resurrect(self):
        self.merge(make_snap(participants=[add("collab", 1000.0)]))
        self.merge(make_snap(removals=[tomb("collab", 2000.0)]))
        # An old snapshot (pre-removal roster, no tombstones) is
        # re-delivered late: the stored tombstone still wins.
        task = self.merge(make_snap(
            participants=[add("collab", 1000.0)]))
        self.assertEqual(roster(task), {"lead": "lead"})

    def test_newer_readd_revives(self):
        self.merge(make_snap(participants=[add("collab", 1000.0)]))
        self.merge(make_snap(removals=[tomb("collab", 2000.0)]))
        task = self.merge(make_snap(
            participants=[add("collab", 3000.0)],
            removals=[tomb("collab", 2000.0)]))
        self.assertEqual(roster(task),
                         {"lead": "lead", "collab": "collaborator"})
        added = {p["agent_id"]: p["added_at"]
                 for p in task["participants"]}
        self.assertEqual(added["collab"], 3000.0)

    def test_removal_union_keeps_latest(self):
        self.merge(make_snap(participants=[add("collab", 1000.0)]))
        task = self.merge(make_snap(removals=[tomb("collab", 1500.0)]))
        self.assertEqual(
            removals_by_id(task)["collab"]["removed_at"], 1500.0)
        # An older tombstone arriving later must not rewind the union.
        task = self.merge(make_snap(removals=[tomb("collab", 1200.0)]))
        self.assertEqual(
            removals_by_id(task)["collab"]["removed_at"], 1500.0)
        task = self.merge(make_snap(
            removals=[tomb("collab", 1800.0, removed_by="owner")]))
        latest = removals_by_id(task)["collab"]
        self.assertEqual(latest["removed_at"], 1800.0)
        self.assertEqual(latest["removed_by"], "owner")

    def test_tombstone_blocks_stale_add_for_unknown_agent(self):
        # The removal reaches this node before the agent was ever a
        # participant here: the tombstone is stored on its own, and a
        # stale add older than it still loses.
        self.merge(make_snap(removals=[tomb("ghost", 5000.0)]))
        task = self.merge(make_snap(
            participants=[add("ghost", 4500.0)]))
        self.assertNotIn("ghost", roster(task))
        task = self.merge(make_snap(
            participants=[add("ghost", 6000.0)]))
        self.assertIn("ghost", roster(task))

    def test_lead_immune_to_tombstones(self):
        task = self.merge(make_snap(
            participants=[add("lead", 900.0, "lead")],
            removals=[tomb("lead", 9999.0)]))
        self.assertEqual(roster(task).get("lead"), "lead")


class TestLocalRemovalTombstone(unittest.TestCase):
    """The local store path: remove_participant records the tombstone
    (with who removed), and a local re-add revives the participant."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agent-mesh-p10-local-")
        self.store = TaskStore(os.path.join(self.dir, "tasks.db"))
        task, _ = self.store.create(
            "Local task", "req", assignee_agent="lead",
            participants=["collab"])
        self.tid = task["id"]
        self.store.transition(self.tid, "SENT")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_remove_records_tombstone_and_readd_revives(self):
        before = time.time()
        task = self.store.remove_participant(
            self.tid, "collab", removed_by="req")
        self.assertEqual(roster(task), {"lead": "lead"})
        tombstone = removals_by_id(task)["collab"]
        self.assertEqual(tombstone["removed_by"], "req")
        self.assertGreaterEqual(tombstone["removed_at"], before)
        self.assertLessEqual(tombstone["removed_at"], time.time())
        self.assertFalse(self.store.is_participant(self.tid, "collab"))
        task = self.store.add_participant(self.tid, "collab", "req")
        self.assertEqual(roster(task),
                         {"lead": "lead", "collab": "collaborator"})
        self.assertTrue(self.store.is_participant(self.tid, "collab"))

    def test_lead_removal_still_refused(self):
        with self.assertRaises(ValueError):
            self.store.remove_participant(
                self.tid, "lead", removed_by="req")
        self.assertEqual(roster(self.store.get(self.tid)),
                         {"lead": "lead", "collab": "collaborator"})
        self.assertEqual(
            self.store.get(self.tid)["participant_removals"], [])


# -- two-process federation (mirrors the Phase 9 round-trip) ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_node_process(port, data_dir, name, admin):
    env = dict(os.environ)
    env.update({
        "AGENT_MESH_ADMIN": admin,
        "AGENT_MESH_MASTER": "test-master-passphrase-p10",
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


def raw_admin(base, method, path, admin):
    """Owner call the way the GUI makes it (X-Admin-Token header).
    Returns (status, parsed_json)."""
    req = urllib.request.Request(base + path, method=method)
    req.add_header("X-Admin-Token", admin)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


ADMIN_A = "test-admin-token-p10-a"
ADMIN_B = "test-admin-token-p10-b"


class TestFederatedRemoval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir_a = tempfile.mkdtemp(prefix="agent-mesh-p10a-")
        cls.dir_b = tempfile.mkdtemp(prefix="agent-mesh-p10b-")
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

    def test_removal_propagates_and_readd_revives(self):
        out = self.a_req.create_task(
            title="Removal propagation task",
            instructions="lead on A, collaborator on B",
            assignee_agent=self.a_lead_id,
            participants=[self.b_collab_id])
        origin_id = out["task"]["id"]
        self.a_req.transition(origin_id, "SENT")

        def b_copy():
            tasks = self.b_collab.list_tasks(
                participant=self.b_collab_id)["tasks"]
            mine = [t for t in tasks
                    if t["title"] == "Removal propagation task"]
            return mine[0] if mine else None

        wait_for(lambda: b_copy() is not None,
                 desc="task visible to the collaborator on node B")
        copy_id = b_copy()["id"]

        # Remove the collaborator on A. The tombstone must reach B:
        # B's copy drops them from the roster, and B's agent loses
        # read access to the task entirely (403).
        self.a_req.remove_participant(origin_id, self.b_collab_id)
        origin = self.a_req.get_task(origin_id)["task"]
        self.assertNotIn(self.b_collab_id, roster(origin))
        self.assertEqual(
            removals_by_id(origin)[self.b_collab_id]["removed_by"],
            self.a_req_id)

        def b_roster_dropped():
            status, body = raw_admin(self.base_b, "GET",
                                     f"/tasks/{copy_id}", ADMIN_B)
            if status != 200:
                return False
            task = body["task"]
            return self.b_collab_id not in roster(task) and \
                self.b_collab_id in removals_by_id(task)
        wait_for(b_roster_dropped,
                 desc="B's copy dropped the removed collaborator")

        def b_locked_out():
            try:
                self.b_collab.get_task(copy_id)
                return False
            except MeshError as exc:
                return exc.status == 403
        wait_for(b_locked_out,
                 desc="removed collaborator gets 403 on node B")
        self.assertIsNone(b_copy())

        # Re-adding on A revives the participant on B as well.
        self.a_req.add_participant(origin_id, self.b_collab_id)

        def b_restored():
            try:
                task = self.b_collab.get_task(copy_id)["task"]
            except MeshError:
                return False
            return roster(task).get(self.b_collab_id) == "collaborator"
        wait_for(b_restored,
                 desc="re-added collaborator restored on node B")


if __name__ == "__main__":
    unittest.main()
