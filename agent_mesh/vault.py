"""Encrypted vault v2 + broker permission check (Phase 1).

Crypto path (checked on this VM, 2026-11-15): the `cryptography` package is
NOT importable in the system python3 (externally-managed environment), so
the default path here is the documented fallback — OpenSSL CLI
AES-256-CBC encrypt-then-MAC, keys derived with PBKDF2-HMAC-SHA256
(600k iterations, per-store random salt) via stdlib hashlib, integrity via
stdlib HMAC-SHA256 (same pattern as Susan's GovIntel credstore). If
`cryptography` IS importable, Fernet is used instead. Either way the blob
is prefixed with its scheme (`fernet:` / `openssl:`) so decryption works
regardless of which path wrote it, and plaintext secrets are never
written to disk, logs, audit entries, or API responses.
"""
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import secrets as secrets_mod
import subprocess
import time
from typing import Any, Dict, Optional

try:  # Preferred path when available.
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except Exception:  # pragma: no cover - depends on environment
    Fernet = None  # type: ignore
    _HAS_FERNET = False

ACTIONS = ("read", "write", "spend")
_PBKDF2_ROUNDS = 600_000

# Credential types (v2.1). A "service" is any shared credential; its
# type decides which fields are metadata (listable, never secret) and
# what a brokered `use` may hand back:
#   api_key / oauth_token — secret is the key/token; upstream is meta.
#   login                 — secret is the password; username+url meta.
#   ssh                   — secret is the key or password; host/user/
#                           port are meta.
#   generic               — secret is opaque; a label is meta.
# In `use` mode the secret NEVER leaves the node for any type: login /
# ssh uses return only the non-secret connection details, api uses are
# brokered by the node's existing use-check flow.
CRED_TYPES = ("api_key", "login", "ssh", "oauth_token", "generic")
GRANT_MODES = ("use", "reveal")


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode())


def load_master_passphrase(explicit: Optional[str] = None) -> str:
    """Resolution order: argument, $AGENT_MESH_MASTER, $AGENT_MESH_MASTER_FILE
    (or ~/.agent-mesh/master.key), else raise. Key files should be 0600."""
    if explicit:
        return explicit
    env = os.environ.get("AGENT_MESH_MASTER")
    if env:
        return env
    key_file = os.environ.get("AGENT_MESH_MASTER_FILE") or os.path.expanduser(
        "~/.agent-mesh/master.key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    raise RuntimeError(
        "no master passphrase: set AGENT_MESH_MASTER or AGENT_MESH_MASTER_FILE")


class Vault:
    def __init__(self, store_path: str, audit_path: str,
                 passphrase: Optional[str] = None):
        self.store_path = store_path
        self.audit_path = audit_path
        # Optional approvals store (Phase 3): when set, an approved and
        # not-yet-consumed approval lets one gated use through.
        self.approvals = None
        self._passphrase = load_master_passphrase(passphrase)
        os.makedirs(os.path.dirname(os.path.abspath(store_path)),
                    exist_ok=True)
        if not os.path.exists(store_path):
            self._save({"version": 2,
                        "salt": _b64e(secrets_mod.token_bytes(16)),
                        "services": {}})
        self._data = self._load()
        # Derive once per store salt.
        salt = _b64d(self._data["salt"])
        material = hashlib.pbkdf2_hmac(
            "sha256", self._passphrase.encode(), salt, _PBKDF2_ROUNDS, dklen=64)
        self._enc_key, self._mac_key = material[:32], material[32:]

    # -- storage ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        with open(self.store_path) as f:
            return json.load(f)

    def _save(self, data: Optional[Dict[str, Any]] = None) -> None:
        data = data if data is not None else self._data
        tmp = self.store_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.store_path)
        try:
            os.chmod(self.store_path, 0o600)
        except Exception:
            pass

    # -- crypto ----------------------------------------------------------
    def _encrypt(self, plaintext: str) -> str:
        if _HAS_FERNET:
            key = base64.urlsafe_b64encode(self._enc_key)
            return "fernet:" + Fernet(key).encrypt(
                plaintext.encode()).decode()
        iv = secrets_mod.token_bytes(16)
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-K", self._enc_key.hex(),
             "-iv", iv.hex()],
            input=plaintext.encode(), capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("openssl encryption failed")
        ct = proc.stdout
        mac = hmac_mod.new(self._mac_key, iv + ct, hashlib.sha256).digest()
        return "openssl:" + _b64e(iv + mac + ct)

    def _decrypt(self, blob: str) -> str:
        scheme, _, payload = blob.partition(":")
        if scheme == "fernet":
            if not _HAS_FERNET:
                raise RuntimeError("blob needs `cryptography`, not installed")
            key = base64.urlsafe_b64encode(self._enc_key)
            return Fernet(key).decrypt(payload.encode()).decode()
        if scheme != "openssl":
            raise RuntimeError(f"unknown vault blob scheme: {scheme!r}")
        raw = _b64d(payload)
        iv, mac, ct = raw[:16], raw[16:48], raw[48:]
        expected = hmac_mod.new(self._mac_key, iv + ct, hashlib.sha256).digest()
        if not hmac_mod.compare_digest(mac, expected):
            raise RuntimeError("vault integrity check failed (wrong "
                               "passphrase or tampered store)")
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-K", self._enc_key.hex(),
             "-iv", iv.hex()],
            input=ct, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("openssl decryption failed")
        return proc.stdout.decode()

    # -- services --------------------------------------------------------
    def set_service(self, service: str, secret: str, upstream: str = "",
                    acl: Optional[Dict[str, Any]] = None,
                    requires_approval: bool = False,
                    cred_type: str = "api_key",
                    meta: Optional[Dict[str, Any]] = None) -> None:
        if cred_type not in CRED_TYPES:
            raise ValueError(
                f"unknown credential type {cred_type!r}; "
                f"allowed {CRED_TYPES}")
        clean_acl: Dict[str, Any] = {}
        for agent_id, actions in (acl or {}).items():
            bad = [a for a in actions if a not in ACTIONS]
            if bad:
                raise ValueError(f"unknown actions {bad}; allowed {ACTIONS}")
            clean_acl[agent_id] = list(actions)
        previous = self._data["services"].get(service, {})
        self._data["services"][service] = {
            "upstream": upstream,
            "acl": clean_acl,
            "requires_approval": bool(requires_approval),
            "ciphertext": self._encrypt(secret),
            "type": cred_type,
            "meta": dict(meta or {}),
            # Re-saving a credential keeps its sharing grants.
            "grants": previous.get("grants", {}),
        }
        self._save()

    def update_service(self, service: str, acl: Optional[Dict[str, Any]] = None,
                       requires_approval: Optional[bool] = None,
                       upstream: Optional[str] = None,
                       meta: Optional[Dict[str, Any]] = None) -> None:
        """Change a service's ACL / approval flag / upstream / metadata
        WITHOUT touching its stored secret (GUI ACL editor path)."""
        cfg = self._data["services"].get(service)
        if cfg is None:
            raise KeyError(f"service not found: {service}")
        if acl is not None:
            clean_acl: Dict[str, Any] = {}
            for agent_id, actions in acl.items():
                bad = [a for a in actions if a not in ACTIONS]
                if bad:
                    raise ValueError(
                        f"unknown actions {bad}; allowed {ACTIONS}")
                clean_acl[agent_id] = list(actions)
            cfg["acl"] = clean_acl
        if requires_approval is not None:
            cfg["requires_approval"] = bool(requires_approval)
        if upstream is not None:
            cfg["upstream"] = upstream
        if meta is not None:
            cfg["meta"] = dict(meta)
        self._save()

    def delete_service(self, service: str) -> bool:
        if service not in self._data["services"]:
            return False
        del self._data["services"][service]
        self._save()
        return True

    def get_secret(self, service: str) -> Optional[str]:
        """Node-internal broker use ONLY. Never expose over HTTP."""
        cfg = self._data["services"].get(service)
        return self._decrypt(cfg["ciphertext"]) if cfg else None

    def list_services_safe(self) -> Dict[str, Any]:
        return {
            name: {"upstream": cfg.get("upstream", ""),
                   "acl": cfg.get("acl", {}),
                   "requires_approval": cfg.get("requires_approval", False),
                   "has_secret": True,
                   # v2.1: type + non-secret metadata + sharing grants
                   # are listable; secret values never are.
                   "type": cfg.get("type", "api_key"),
                   "meta": cfg.get("meta", {}),
                   "grants": cfg.get("grants", {})}
            for name, cfg in self._data["services"].items()
        }

    # -- sharing grants (v2.1) --------------------------------------------
    def set_grant(self, service: str, agent_id: str, mode: str,
                  standing_reveal: bool = False) -> Dict[str, Any]:
        """Owner grants one agent use of one credential. mode 'use' =
        brokered (the secret never leaves the node); mode 'reveal' =
        the agent may receive the secret value, per-use approved unless
        the grant is marked standing_reveal."""
        cfg = self._data["services"].get(service)
        if cfg is None:
            raise KeyError(f"service not found: {service}")
        if mode not in GRANT_MODES:
            raise ValueError(
                f"grant mode must be one of {GRANT_MODES}, got {mode!r}")
        grant = {"mode": mode,
                 "standing_reveal": bool(standing_reveal)
                 if mode == "reveal" else False}
        cfg.setdefault("grants", {})[agent_id] = grant
        self._save()
        return grant

    def remove_grant(self, service: str, agent_id: str) -> bool:
        cfg = self._data["services"].get(service)
        if cfg is None:
            raise KeyError(f"service not found: {service}")
        grants = cfg.setdefault("grants", {})
        if agent_id not in grants:
            return False
        del grants[agent_id]
        self._save()
        return True

    def get_grant(self, service: str,
                  agent_id: str) -> Optional[Dict[str, Any]]:
        cfg = self._data["services"].get(service)
        if cfg is None:
            return None
        return cfg.get("grants", {}).get(agent_id)

    # -- broker check ----------------------------------------------------
    def audit(self, agent: str, service: str, action: str, outcome: str) -> None:
        entry = {"ts": time.time(), "agent": agent, "service": service,
                 "action": action, "outcome": outcome}
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def use_check(self, agent_id: str, service: str, action: str) -> str:
        """Returns 'allow' | 'deny' | 'needs_approval'; always audited."""
        cfg = self._data["services"].get(service)
        if cfg is None or action not in ACTIONS:
            outcome = "deny"
        elif action not in cfg.get("acl", {}).get(agent_id, []):
            outcome = "deny"
        elif cfg.get("requires_approval"):
            # One approved, unconsumed approval lets exactly one use
            # through; after that the service is gated again.
            if self.approvals is not None and self.approvals.consume_approved(
                    agent_id, service, action):
                outcome = "allow"
            else:
                outcome = "needs_approval"
        else:
            outcome = "allow"
        self.audit(agent_id, service, action, outcome)
        return outcome

    # -- credential sharing (v2.1) -----------------------------------------
    def credential_use(self, agent_id: str, service: str) -> Dict[str, Any]:
        """Brokered use of a shared credential under a `use` grant.

        The secret NEVER leaves the node: api credentials are used
        through the node's broker flow (this returns the decision plus
        the non-secret upstream), and login / ssh credentials return
        only their non-secret connection details (host, user, url,
        port) so the agent knows where to connect — the password / key
        itself is never in the response. Always audited."""
        cfg = self._data["services"].get(service)
        grant = self.get_grant(service, agent_id)
        if cfg is None or grant is None or grant.get("mode") != "use":
            self.audit(agent_id, service, "use", "deny")
            return {"outcome": "deny", "service": service}
        cred_type = cfg.get("type", "api_key")
        meta = cfg.get("meta", {})
        out: Dict[str, Any] = {"outcome": "allow", "service": service,
                               "type": cred_type, "mode": "use"}
        if cred_type == "login":
            out["username"] = meta.get("username", "")
            out["url"] = meta.get("url", "") or cfg.get("upstream", "")
        elif cred_type == "ssh":
            out["host"] = meta.get("host", "")
            out["user"] = meta.get("user", "")
            out["port"] = meta.get("port", 22)
        elif cred_type in ("api_key", "oauth_token"):
            out["upstream"] = cfg.get("upstream", "")
            out["note"] = ("brokered by the node: present this decision "
                           "to the node's broker flow; the key itself "
                           "never leaves the node")
        else:  # generic
            out["label"] = meta.get("label", "")
            out["note"] = ("brokered by the node; the stored value "
                           "never leaves the node in use mode")
        self.audit(agent_id, service, "use", "allow")
        return out

    def reveal(self, agent_id: str, service: str) -> Dict[str, Any]:
        """Hand the secret value to an agent holding a `reveal` grant.

        Per-use approval is required unless the grant is standing.
        Approval uses the same one-shot ApprovalStore as gated service
        uses, with action 'reveal': an approved, unconsumed request
        lets exactly one reveal through, then it re-gates. EVERY
        reveal attempt is audit-logged (agent, credential name,
        outcome — never the value)."""
        cfg = self._data["services"].get(service)
        grant = self.get_grant(service, agent_id)
        if cfg is None or grant is None or grant.get("mode") != "reveal":
            self.audit(agent_id, service, "reveal", "deny")
            return {"outcome": "deny", "service": service}
        if grant.get("standing_reveal"):
            self.audit(agent_id, service, "reveal", "allow_standing")
            return {"outcome": "allow", "service": service,
                    "type": cfg.get("type", "api_key"),
                    "secret": self._decrypt(cfg["ciphertext"])}
        if self.approvals is not None and self.approvals.consume_approved(
                agent_id, service, "reveal"):
            self.audit(agent_id, service, "reveal", "allow_approved")
            return {"outcome": "allow", "service": service,
                    "type": cfg.get("type", "api_key"),
                    "secret": self._decrypt(cfg["ciphertext"])}
        self.audit(agent_id, service, "reveal", "needs_approval")
        return {"outcome": "needs_approval", "service": service}
