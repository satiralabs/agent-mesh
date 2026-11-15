"""Small client for the Phase 1 node API — drive the flow without curl."""
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class MeshError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class MeshClient:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 admin_token: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.admin_token = admin_token

    def _call(self, method: str, path: str, body: Optional[Dict] = None,
              admin: bool = False) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if admin and self.admin_token:
            req.add_header("X-Admin-Token", self.admin_token)
        elif self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw or b"{}")
            except Exception:
                parsed = {"raw": raw.decode(errors="replace")}
            raise MeshError(exc.code, parsed) from exc

    # agents
    def register(self, **kw) -> Dict[str, Any]:
        out = self._call("POST", "/agents/register", kw, admin=True)
        self.token = out["token"]
        return out

    def heartbeat(self, status: Optional[str] = None):
        return self._call("POST", "/agents/heartbeat",
                          {"status": status} if status else {})

    def list_agents(self):
        return self._call("GET", "/agents")

    # messages
    def send_message(self, recipient: str, msg_type: str, payload: Any = "",
                     allow_secret_risk: bool = False):
        body: Dict[str, Any] = {"recipient": recipient, "msg_type": msg_type,
                                "payload": payload}
        if allow_secret_risk:
            body["allow_secret_risk"] = True
        return self._call("POST", "/messages", body)

    def list_messages(self, recipient: Optional[str] = None):
        q = f"?recipient={recipient}" if recipient else ""
        return self._call("GET", "/messages" + q)

    # tasks
    def create_task(self, **kw):
        return self._call("POST", "/tasks", kw)

    def get_task(self, task_id: str):
        return self._call("GET", f"/tasks/{task_id}")

    def list_tasks(self, **filters):
        q = "&".join(f"{k}={v}" for k, v in filters.items() if v)
        return self._call("GET", "/tasks" + (f"?{q}" if q else ""))

    def transition(self, task_id: str, to: str, result: Optional[str] = None):
        body: Dict[str, Any] = {"to": to}
        if result is not None:
            body["result"] = result
        return self._call("POST", f"/tasks/{task_id}/transition", body)

    def progress(self, task_id: str, note: str):
        return self._call("POST", f"/tasks/{task_id}/progress", {"note": note})

    # collaboration (v2.2): participants + the shared comment thread
    def add_participant(self, task_id: str, agent_id: str):
        return self._call("POST", f"/tasks/{task_id}/participants",
                          {"agent_id": agent_id})

    def remove_participant(self, task_id: str, agent_id: str):
        return self._call("DELETE",
                          f"/tasks/{task_id}/participants/{agent_id}")

    def add_comment(self, task_id: str, body: str):
        return self._call("POST", f"/tasks/{task_id}/comments",
                          {"body": body})

    # vault
    def vault_add_service(self, **kw):
        return self._call("POST", "/vault/services", kw, admin=True)

    def vault_services(self):
        return self._call("GET", "/vault/services")

    def vault_use_check(self, service: str, action: str = "read"):
        return self._call("POST", "/vault/use-check",
                          {"service": service, "action": action})

    # vault admin (Phase 3 GUI paths)
    def vault_update_service(self, service: str, **kw):
        return self._call("PUT", f"/vault/services/{service}", kw, admin=True)

    def vault_delete_service(self, service: str):
        return self._call("DELETE", f"/vault/services/{service}", admin=True)

    # vault credential sharing (v2.1)
    def vault_grant(self, service: str, agent_id: str, mode: str,
                    standing_reveal: bool = False):
        return self._call("POST", f"/vault/services/{service}/grants",
                          {"agent_id": agent_id, "mode": mode,
                           "standing_reveal": standing_reveal}, admin=True)

    def vault_revoke_grant(self, service: str, agent_id: str):
        return self._call("DELETE",
                          f"/vault/services/{service}/grants/{agent_id}",
                          admin=True)

    def vault_credential_use(self, service: str):
        return self._call("POST", "/vault/credential-use",
                          {"service": service})

    def vault_reveal(self, service: str):
        return self._call("POST", "/vault/reveal", {"service": service})

    # handover (v2.1)
    def handover_rules(self):
        return self._call("GET", "/handover/rules")

    def handover_log(self, **kw):
        return self._call("POST", "/handover/entries", kw)

    def handover_entries(self):
        return self._call("GET", "/handover/entries")

    # approvals (Phase 3)
    def approvals_list(self, status: Optional[str] = None):
        q = f"?status={status}" if status else ""
        return self._call("GET", "/approvals" + q, admin=True)

    def approval_decide(self, approval_id: str, approve: bool):
        return self._call("POST", f"/approvals/{approval_id}/decide",
                          {"decision": "approve" if approve else "deny"},
                          admin=True)

    def approval_request(self, service: str, action: str = "read"):
        return self._call("POST", "/approvals",
                          {"service": service, "action": action})

    # activity + packs (Phase 3)
    def activity(self, limit: int = 100):
        return self._call("GET", f"/activity?limit={limit}", admin=True)

    def agent_pack(self, agent_id: str):
        return self._call("POST", f"/agents/{agent_id}/pack", {}, admin=True)

    def disconnect_agent(self, agent_id: str):
        """Phase 6: admin disconnect — revokes the agent's token."""
        return self._call("POST", f"/agents/{agent_id}/disconnect", {},
                          admin=True)

    def register_with_pack(self, **kw):
        return self._call("POST", "/agents/pack", kw, admin=True)

    # peers / federation (Phase 4, admin)
    def node_info(self):
        return self._call("GET", "/node/info", admin=True)

    def pairing_code(self):
        return self._call("POST", "/peers/pairing-code", {}, admin=True)

    def pair_with(self, peer_url: str, code: str):
        return self._call("POST", "/peers/pair",
                          {"peer_url": peer_url, "code": code}, admin=True)

    def peers_list(self):
        return self._call("GET", "/peers", admin=True)

    def peer_sync(self, peer_node_id: str):
        return self._call("POST", f"/peers/{peer_node_id}/sync", {},
                          admin=True)

    def peers_flush(self):
        return self._call("POST", "/peers/flush", {}, admin=True)

    def peers_outbox(self):
        return self._call("GET", "/peers/outbox", admin=True)
