import json
import os
import stat
from typing import Dict, Any, Optional

DEFAULT_SECRETS_PATH = os.path.expanduser("~/.agent-mesh/secrets.json")

def ensure_secure_file(path: str = DEFAULT_SECRETS_PATH) -> None:
    parent_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent_dir, exist_ok=True)
    # Ensure directory is 0700
    try:
        os.chmod(parent_dir, stat.S_IRWXU)
    except Exception:
        pass
        
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"services": {}}, f, indent=2)
            
    # Ensure file is 0600 (owner read/write only)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

def load_secrets(path: str = DEFAULT_SECRETS_PATH) -> Dict[str, Any]:
    ensure_secure_file(path)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"services": {}}

def save_secrets(data: Dict[str, Any], path: str = DEFAULT_SECRETS_PATH) -> None:
    ensure_secure_file(path)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

def set_service_secret(
    service: str,
    upstream: str,
    token: str,
    auth_type: str = "bearer",
    header_name: str = "Authorization",
    require_approval: bool = False,
    path: str = DEFAULT_SECRETS_PATH
) -> None:
    data = load_secrets(path)
    services = data.setdefault("services", {})
    services[service] = {
        "upstream": upstream.rstrip("/"),
        "auth_type": auth_type,
        "header_name": header_name,
        "token": token,
        "require_approval": require_approval
    }
    save_secrets(data, path)

def get_service_config(service: str, path: str = DEFAULT_SECRETS_PATH) -> Optional[Dict[str, Any]]:
    data = load_secrets(path)
    return data.get("services", {}).get(service)

def list_services_safe(path: str = DEFAULT_SECRETS_PATH) -> Dict[str, Any]:
    data = load_secrets(path)
    safe = {}
    for svc, cfg in data.get("services", {}).items():
        raw_tok = cfg.get("token", "")
        if len(raw_tok) > 6:
            masked = raw_tok[:3] + "..." + raw_tok[-3:]
        elif raw_tok:
            masked = "***"
        else:
            masked = "(none)"
        safe[svc] = {
            "upstream": cfg.get("upstream"),
            "auth_type": cfg.get("auth_type"),
            "masked_token": masked,
            "require_approval": cfg.get("require_approval", False)
        }
    return safe

def delete_service(service: str, path: str = DEFAULT_SECRETS_PATH) -> bool:
    data = load_secrets(path)
    if "services" in data and service in data["services"]:
        del data["services"][service]
        save_secrets(data, path)
        return True
    return False
