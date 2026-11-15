"""Secret scanner (v2 Phase 5, plan §4b Tier 1; hardened in v2.1.1).

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
- ``aws_access_key``   AKIA + 16 upper/digits (any case for the tail)
- ``slack_token``      xox[baprs]-...
- ``bearer_token``     "Bearer <20+ token chars>"
- ``jwt``              eyJ....<two more base64url segments>
- ``secret_assignment``  password|api_key|secret|token|... = <value>
  where the value is not an obvious placeholder (YOUR_..., <...>,
  ${...}, xxx, ***, changeme, example, redacted, ...). The password
  family (password|passwd|pwd) is held to a lower bar — values of
  >=4 chars — because real passwords are often short.

v2.1.1 hardening (audit F2 — the scanner used to be purely
shape-based, so obfuscated credentials slipped through):

- Zero-width characters (ZWSP/ZWNJ/ZWJ/word joiner/BOM/soft hyphen)
  are stripped before scanning, so they cannot break up a keyword
  or a token prefix.
- Prefixed tokens (ghp_, sk-, AKIA, xox-) are also matched by a
  "walk" that tolerates a few single separators (spaces, tabs,
  newlines, dots) between token segments — a token split by a space
  or a newline, or an sk- key with dots between its segments, is
  still caught. Segments after a separator must be >=4 token chars,
  which keeps ordinary prose (short words) from being stitched into
  a false token.
- Assignment keywords tolerate a space inside them ("api key:").
- Base64-looking runs (>=20 chars) are decoded and the decoded text
  is rescanned, one level deep. Decodes that are not clean printable
  UTF-8 are ignored, so hashes and ids do not false-positive.

Known residuals (documented, accepted): a Bearer token split by
spaces, a password value shorter than 4 chars, a token split
immediately after its prefix ("ghp_ XXXX..."), and separators
inserted every 1–3 chars inside a token still pass. The scanner is
a guardrail against careless leaks, not a guarantee against a
determined smuggler — the vault is the actual security boundary.

Plain prose mentioning the word "token" (or "secret", "password")
without an assignment or a credential shape does NOT match.
"""
import base64
import binascii
import re
import string
from typing import Dict, List, Set, Tuple

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

# Invisible characters used to break shapes up (audit F2): zero-width
# space / non-joiner / joiner, word joiner, BOM, soft hyphen.
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# -- separator-tolerant token walks --------------------------------------
_CH_ALNUM = frozenset(string.ascii_letters + string.digits)
_CH_ALNUM_DASH = _CH_ALNUM | frozenset("-_")
_CH_AWS = frozenset(string.digits + string.ascii_uppercase)
_CH_SLACK = _CH_ALNUM | frozenset("-")
_CH_JWT = _CH_ALNUM_DASH | frozenset(".")

_MAX_SEPS = 4       # separators tolerated inside one token
_MIN_PIECE = 4      # token chars required after each separator


def _walk_token(text: str, start: int, chars: frozenset,
                threshold: int, first_sep_ok: bool = False,
                dot_seps: bool = True) -> Tuple[int, int]:
    """Consume a token starting at `start` (just past a known prefix).

    Counts token characters, tolerating up to _MAX_SEPS single
    separators (a whitespace run, or one dot) between segments; each
    segment after a separator must have >= _MIN_PIECE token chars, so
    prose word chains do not stitch into a fake token. The first
    segment must start immediately at `start` unless `first_sep_ok`
    (used only for the all-caps AKIA shape, whose character set is
    itself a strong signal). Returns (token_chars, end_index)."""
    n = len(text)
    i = start
    seps = 0
    if first_sep_ok and i < n and text[i] in " \t\r\n":
        while i < n and text[i] in " \t\r\n":
            i += 1
        seps = 1
    j = i
    while j < n and text[j] in chars:
        j += 1
    if j == i:
        return 0, start
    total = j - i
    i = j
    while total < threshold and seps < _MAX_SEPS:
        k = i
        if k < n and text[k] in " \t\r\n":
            while k < n and text[k] in " \t\r\n":
                k += 1
        elif dot_seps and k < n and text[k] == ".":
            k += 1
        else:
            break
        m = k
        while m < n and text[m] in chars:
            m += 1
        if m - k < _MIN_PIECE:
            break
        seps += 1
        total += m - k
        i = m
    return total, i


# (rule, prefix regex, token chars, threshold, first_sep_ok)
_PREFIX_WALKS = [
    ("github_token", re.compile(r"\bgh[posru]_"), _CH_ALNUM, 20, False),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?"), _CH_ALNUM_DASH, 20,
     False),
    ("aws_access_key", re.compile(r"\bAKIA"), _CH_AWS, 16, True),
    ("aws_access_key", re.compile(r"\bakia", re.IGNORECASE), _CH_ALNUM,
     16, False),
    ("slack_token", re.compile(r"\bxox[baprs]-"), _CH_SLACK, 10, False),
]

_JWT_PREFIX = re.compile(r"\beyJ")


def _walk_hits(text: str) -> Set[str]:
    found: Set[str] = set()
    for name, prefix, chars, threshold, first_sep_ok in _PREFIX_WALKS:
        for m in prefix.finditer(text):
            total, _ = _walk_token(text, m.end(), chars, threshold,
                                   first_sep_ok=first_sep_ok)
            if total >= threshold:
                found.add(name)
                break
    for m in _JWT_PREFIX.finditer(text):
        total, end = _walk_token(text, m.end(), _CH_JWT, 40,
                                 dot_seps=False)
        if total >= 40 and text[m.end():end].count(".") >= 2:
            found.add("jwt")
            break
    return found


# -- assignments -----------------------------------------------------------
_KEYWORD = (r"(?:password|passwd|pwd|api[\s_-]?key|apikey|secret|"
            r"secret[\s_-]?key|access[\s_-]?token|auth[\s_-]?token|token|"
            r"private[\s_-]?key)")
_ASSIGNMENT = re.compile(
    r"\b" + _KEYWORD + r"\b\s*[:=]\s*[\"']?([^\s\"',;]{8,})",
    re.IGNORECASE)
# The password family gets a lower threshold: real passwords are
# often shorter than 8 chars, and "password = <word>" is rarely
# innocent prose once the value clears the placeholder checks.
_ASSIGNMENT_PW = re.compile(
    r"\b(?:password|passwd|pwd)\b\s*[:=]\s*[\"']?([^\s\"',;]{4,})",
    re.IGNORECASE)

_PLACEHOLDER_PREFIXES = (
    "your", "<", "${", "{", "xxx", "***", "...", "change", "example",
    "placeholder", "redacted", "[redacted", "insert", "todo", "none",
    "null", "dummy-", "sample", "test-", "fake-",
)

# Whole values that are plainly not secrets (config booleans and
# states). Exact match only — a prefix match here would wave through
# real values that happen to start with the same letters.
_NON_SECRET_VALUES = frozenset({
    "true", "false", "yes", "no", "on", "off", "enabled", "disabled",
    "required", "optional", "default", "empty", "blank", "n/a", "nil",
    "undefined", "missing", "unset", "tbd",
})


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'").lower()
    if not v:
        return True
    if v in _NON_SECRET_VALUES:
        return True
    return any(v.startswith(p) for p in _PLACEHOLDER_PREFIXES)


# -- base64 decode-and-rescan ----------------------------------------------
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}|[A-Za-z0-9_-]{20,}={0,2}")
_MAX_B64_CANDIDATES = 20
_MAX_B64_DECODED = 65536


def _b64_candidates(text: str) -> List[str]:
    """Base64-looking runs, from the text as-is and from a
    whitespace-stripped copy (which rejoins line-wrapped blobs)."""
    seen: List[str] = []
    for src in (text, re.sub(r"\s+", "", text)):
        for m in _B64_RUN.finditer(src):
            cand = m.group(0)
            if cand not in seen:
                seen.append(cand)
                if len(seen) >= _MAX_B64_CANDIDATES:
                    return seen
    return seen


def _try_decode(cand: str):
    """Decode a base64 candidate to text, or None. Only clean,
    printable UTF-8 survives — random binary (hashes, ids) decodes
    to neither, which keeps false positives near zero."""
    core = cand.rstrip("=")
    if len(core) % 4 == 1:
        return None
    padded = core + "=" * (-len(core) % 4)
    try:
        if "+" in cand or "/" in cand:
            raw = base64.b64decode(padded, validate=True)
        else:
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if not 8 <= len(raw) <= _MAX_B64_DECODED:
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\r\n\t")
    if printable / len(s) < 0.95:
        return None
    return s


def scan_text(text, _depth: int = 0) -> List[str]:
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
    clean = _ZERO_WIDTH.sub("", text)
    found: Set[str] = set()
    for name, pattern in _PATTERNS:
        if pattern.search(clean):
            found.add(name)
    found |= _walk_hits(clean)
    for rx in (_ASSIGNMENT, _ASSIGNMENT_PW):
        for m in rx.finditer(clean):
            if not _is_placeholder(m.group(1)):
                found.add("secret_assignment")
                break
        if "secret_assignment" in found:
            break
    if _depth == 0:
        for cand in _b64_candidates(clean):
            decoded = _try_decode(cand)
            if decoded is not None:
                found.update(scan_text(decoded, _depth=1))
    hits = [name for name, _ in _PATTERNS if name in found]
    if "secret_assignment" in found:
        hits.append("secret_assignment")
    return hits


def scan_fields(fields: Dict[str, object]) -> List[str]:
    """Scan several named fields; returns the matched rule names."""
    hits: List[str] = []
    for value in fields.values():
        for rule in scan_text(value):
            if rule not in hits:
                hits.append(rule)
    return hits
