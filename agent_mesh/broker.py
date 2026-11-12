import http.server
import urllib.request
import urllib.error
import json
import uuid
import time
import os
from typing import Dict, Any, Optional

from .secrets import get_service_config, list_services_safe, set_service_secret, delete_service, DEFAULT_SECRETS_PATH
from .bus import send_message, list_messages, DEFAULT_DB_PATH
from .handover import compile_handover_pack, sync_handover_to_drive, probe_local_services, DEFAULT_MEMORY_DIR, DEFAULT_PACK_DIR

DEFAULT_AUDIT_LOG = os.path.expanduser("~/.agent-mesh/audit.log")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}

def write_audit_log(entry: Dict[str, Any], audit_path: str = DEFAULT_AUDIT_LOG) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(audit_path)), exist_ok=True)
    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(audit_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

class BrokerHandler(http.server.BaseHTTPRequestHandler):
    secrets_path: str = DEFAULT_SECRETS_PATH
    audit_path: str = DEFAULT_AUDIT_LOG

    def log_message(self, format, *args):
        pass

    def send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Serve Web UI
        if self.path in ["/", "/index.html"]:
            index_path = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(index_path):
                with open(index_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # UI State API
        if self.path == "/api/state":
            msgs = list_messages(unread_only=False, limit=50, db_path=DEFAULT_DB_PATH)
            svcs = list_services_safe(self.secrets_path)
            probes = probe_local_services([
                {"name": "Local Agent Broker", "port": 8088},
                {"name": "SpotGames Web Lobby", "port": 3020},
                {"name": "SpotGames Standalone Web", "port": 3030},
                {"name": "EverythingSub Reseller Bot", "port": 8095},
            ])

            # Read latest handover preview
            handover_file = os.path.join(DEFAULT_PACK_DIR, "Handover-for-Another-LLM.md")
            handover_text = ""
            if os.path.exists(handover_file):
                try:
                    with open(handover_file, "r") as f:
                        handover_text = f.read()
                except Exception:
                    pass

            # Read recent audit log
            audit_lines = []
            if os.path.exists(self.audit_path):
                try:
                    with open(self.audit_path, "r") as f:
                        lines = f.readlines()
                        audit_lines = [l.strip() for l in lines[-25:]]
                except Exception:
                    pass

            self.send_json(200, {
                "messages": msgs,
                "services": svcs,
                "approvals": PENDING_APPROVALS,
                "probes": probes,
                "handover_preview": handover_text,
                "audit_log": audit_lines
            })
            return

        if self.path == "/health":
            self.send_json(200, {"status": "ok", "broker": "agent-mesh", "timestamp": time.time()})
            return
        if self.path == "/approvals":
            self.send_json(200, {"pending": PENDING_APPROVALS})
            return

        self.handle_proxy("GET")

    def do_POST(self):
        # API: Send Message from UI
        if self.path == "/api/msg/send":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            msg_id = send_message(
                sender=body.get("sender", "user"),
                recipient=body.get("recipient", "all"),
                msg_type=body.get("msg_type", "GENERAL"),
                payload=body.get("payload", "")
            )
            self.send_json(200, {"success": True, "msg_id": msg_id})
            return

        # API: Set Secret from UI
        if self.path == "/api/secret/set":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            set_service_secret(
                service=body.get("service"),
                upstream=body.get("upstream"),
                token=body.get("token"),
                auth_type=body.get("auth_type", "bearer"),
                require_approval=body.get("require_approval", False),
                path=self.secrets_path
            )
            self.send_json(200, {"success": True})
            return

        # API: Delete Secret from UI
        if self.path == "/api/secret/delete":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            delete_service(body.get("service"), path=self.secrets_path)
            self.send_json(200, {"success": True})
            return

        # API: Trigger Handover Sync from UI
        if self.path == "/api/handover/sync":
            compile_handover_pack()
            sync_handover_to_drive()
            self.send_json(200, {"success": True})
            return

        # API: Approve pending request
        if self.path.startswith("/approval/approve/"):
            req_id = self.path.split("/approval/approve/")[1].strip()
            if req_id in PENDING_APPROVALS:
                del PENDING_APPROVALS[req_id]
                write_audit_log({"event": "MANUAL_APPROVAL_GRANTED", "request_id": req_id}, self.audit_path)
                self.send_json(200, {"status": "APPROVED", "request_id": req_id})
            else:
                self.send_json(404, {"error": "Request ID not found"})
            return

        self.handle_proxy("POST")

    def do_PUT(self):
        self.handle_proxy("PUT")

    def do_DELETE(self):
        self.handle_proxy("DELETE")

    def handle_proxy(self, method: str):
        # Path format: /forward/<service>/<subpath>
        clean_path = self.path
        if not clean_path.startswith("/forward/"):
            self.send_json(404, {"error": "Invalid endpoint. Use /forward/<service>/<subpath>"})
            return

        parts = clean_path[len("/forward/"):].split("/", 1)
        service_name = parts[0]
        subpath = "/" + parts[1] if len(parts) > 1 else ""

        # Remove query parameters for route matching
        pure_subpath = subpath.split("?")[0]
        query_suffix = ("?" + subpath.split("?", 1)[1]) if "?" in subpath else ""

        cfg = get_service_config(service_name, self.secrets_path)
        if not cfg:
            self.send_json(404, {"error": f"Service '{service_name}' not configured in agent-mesh secrets."})
            return

        # Read agent body
        content_len = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(content_len) if content_len > 0 else None

        # Check Human-In-The-Loop approval gate
        if cfg.get("require_approval", False):
            auth_header = self.headers.get("X-Approval-Token")
            if not auth_header:
                req_id = str(uuid.uuid4())[:8]
                PENDING_APPROVALS[req_id] = {
                    "service": service_name,
                    "method": method,
                    "subpath": pure_subpath,
                    "created_at": time.time(),
                    "status": "PENDING"
                }
                write_audit_log({
                    "event": "APPROVAL_REQUIRED",
                    "service": service_name,
                    "request_id": req_id,
                    "subpath": pure_subpath
                }, self.audit_path)
                self.send_json(403, {
                    "error": "APPROVAL_REQUIRED",
                    "request_id": req_id,
                    "message": f"Service '{service_name}' requires human approval before proceeding.",
                    "approve_command": f"agent-mesh broker approve {req_id}"
                })
                return

        # Prepare upstream request with injected secret
        upstream_base = cfg["upstream"]
        auth_type = cfg.get("auth_type", "bearer")
        token = cfg.get("token", "")

        target_url = upstream_base + pure_subpath + query_suffix
        req = urllib.request.Request(target_url, data=req_body, method=method)

        # Forward non-sensitive agent headers
        for h_key, h_val in self.headers.items():
            if h_key.lower() not in ["host", "authorization", "x-approval-token", "content-length"]:
                req.add_header(h_key, h_val)

        # Inject upstream credentials
        if auth_type == "bearer":
            req.add_header("Authorization", f"Bearer {token}")
        elif auth_type == "header":
            header_name = cfg.get("header_name", "X-API-Key")
            req.add_header(header_name, token)
        elif auth_type == "telegram_bot":
            # For telegram: api.telegram.org/bot<token>/method
            target_url = f"{upstream_base}/bot{token}{pure_subpath}{query_suffix}"
            req = urllib.request.Request(target_url, data=req_body, method=method)
            for h_key, h_val in self.headers.items():
                if h_key.lower() not in ["host", "authorization", "x-approval-token", "content-length"]:
                    req.add_header(h_key, h_val)

        # Send upstream
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                resp_data = resp.read()
                write_audit_log({
                    "event": "PROXY_SUCCESS",
                    "service": service_name,
                    "method": method,
                    "subpath": pure_subpath,
                    "status_code": resp.status
                }, self.audit_path)

                self.send_response(resp.status)
                for r_key, r_val in resp.headers.items():
                    if r_key.lower() not in ["content-length", "transfer-encoding"]:
                        self.send_header(r_key, r_val)
                self.send_header("Content-Length", str(len(resp_data)))
                self.end_headers()
                self.wfile.write(resp_data)

        except urllib.error.HTTPError as e:
            err_data = e.read()
            write_audit_log({
                "event": "PROXY_UPSTREAM_ERROR",
                "service": service_name,
                "method": method,
                "subpath": pure_subpath,
                "status_code": e.code
            }, self.audit_path)

            self.send_response(e.code)
            for r_key, r_val in e.headers.items():
                if r_key.lower() not in ["content-length", "transfer-encoding"]:
                    self.send_header(r_key, r_val)
            self.send_header("Content-Length", str(len(err_data)))
            self.end_headers()
            self.wfile.write(err_data)
        except Exception as ex:
            write_audit_log({
                "event": "PROXY_EXCEPTION",
                "service": service_name,
                "error": str(ex)
            }, self.audit_path)
            self.send_json(502, {"error": f"Failed to reach upstream service: {str(ex)}"})

def run_broker(host: str = "127.0.0.1", port: int = 8088, secrets_path: str = DEFAULT_SECRETS_PATH, audit_path: str = DEFAULT_AUDIT_LOG):
    BrokerHandler.secrets_path = secrets_path
    BrokerHandler.audit_path = audit_path
    server = http.server.ThreadingHTTPServer((host, port), BrokerHandler)
    print(f"agent-mesh secret broker running on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBroker stopped.")
        server.server_close()
