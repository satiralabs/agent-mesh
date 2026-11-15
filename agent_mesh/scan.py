"""Secret scanner (v2 Phase 5, plan §4b Tier 1).

Scans outgoing content — message text, task title / instructions /
result, progress notes and file-drop payloads — for credential shapes
BEFORE the node stores or forwards it. On a hit the caller rejects the
submission naming only the RULE that matched: the matched text itself
is never put in an error, a log line, or any response (the scanner
returns rule names only, never the match).

Rules:
- ``pem_private_key``  -----BEGIN ... PRIVATE KEY----- blocks
- ``github_token``     ghp_/gho_/ghu_/ghs_/ghr_ + >=20 chars
- ``openai_key``       sk-... / sk-proj-... (>=20 chars after prefix)
- ``aws_access_key``   AKIA + 16 upper/digits
- ``slack_token``      xox[baprs]-...
- ``bearer_token``     "Bearer <20+ token chars>"
- ``jwt``              eyJ....<two more base64url segments>
- ``secret_assignment``  password|api_key|secret|token|... = <value>
  where the value is not an obvious placeholder (YOUR_..., <...>,
  ${...}, xxx, ***, changeme, example, redacted, ...).

Plain prose mentioning the word "token" (or "secret", "password")
without an assignment or a credential shape does NOT match.
"""
import re
from typing import Dict, List

_PATTERNS = [
    ("pem_private_key",
     re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("github_token",
     re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b")),
    ("openai_key",
     re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer_token",
     re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
                r"[A-Za-z0-9_-]{5,}\b")),
]

_ASSIGNMENT = re.compile(
    r"\b(?:password|passwd|pwd|api[_-]?key|apikey|secret|secret[_-]?key|"
    r"access[_-]?token|auth[_-]?token|token|private[_-]?key)\b"
    r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})",
    re.IGNORECASE)

_PLACEHOLDER_PREFIXES = (
    "your", "<", "${", "{", "xxx", "***", "...", "change", "example",
    "placeholder", "redacted", "[redacted", "insert", "todo", "none",
    "null", "dummy-", "sample", "test-", "fake-",
)


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'").lower()
    if not v:
        return True
    return any(v.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def scan_text(text) -> List[str]:
    """Rule names matched by `text` (deduplicated, in rule order).
    Non-string payloads are JSON-dumped first so structured payloads
    are scanned too. Returns [] for clean content."""
    if text is None:
        return []
    if not isinstance(text, str):
        import json
        try:
            text = json.dumps(text)
        except Exception:
            text = str(text)
    hits: List[str] = []
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            hits.append(name)
    for m in _ASSIGNMENT.finditer(text):
        if not _is_placeholder(m.group(1)):
            hits.append("secret_assignment")
            break
    return hits


def scan_fields(fields: Dict[str, object]) -> List[str]:
    """Scan several named fields; returns the matched rule names."""
    hits: List[str] = []
    for value in fields.values():
        for rule in scan_text(value):
            if rule not in hits:
                hits.append(rule)
    return hits
