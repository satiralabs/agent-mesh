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
    def send_message(self, recipient: str, msg_type: str, payload: Any = ""):
        return self._call("POST", "/messages", {
            "recipient": recipient, "msg_type": msg_type, "payload": payload})

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

    # vault
    def vault_add_service(self, **kw):
        return self._call("POST", "/vault/services", kw, admin=True)

    def vault_services(self):
        return self._call("GET", "/vault/services")

    def vault_use_check(self, service: str, action: str = "read"):
        return self._call("POST", "/vault/use-check",
                          {"service": service, "action": action})
