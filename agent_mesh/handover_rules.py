"""Canonical handover rules (v2.1).

The rules text lives in handover_rules.md beside this module and is
read at runtime, so editing the .md (and bumping its Version line) is
the only step needed to change what every newly connected agent is
told. The node delivers a summary of these rules to each agent once
per rules version (see Node.deliver_handover_starter) and serves the
full text at GET /handover/rules.
"""
import os
import re
from typing import Tuple

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "handover_rules.md")
_FALLBACK_VERSION = "1.0"


def rules_text() -> str:
    try:
        with open(_RULES_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ("# Agent Mesh Handover Rules\n\nVersion: "
                + _FALLBACK_VERSION + "\n\n(rules file missing)\n")


def rules_version() -> str:
    match = re.search(r"^Version:\s*(\S+)\s*$", rules_text(),
                      re.MULTILINE)
    return match.group(1) if match else _FALLBACK_VERSION


def starter_message() -> Tuple[str, str]:
    """(version, message text) for the one-time starter delivery: the
    rules summary plus exactly how to file entries."""
    version = rules_version()
    text = (
        f"Welcome to the mesh. Handover is automatic here: this node "
        f"follows handover rules v{version}, and every agent is "
        f"expected to file one handover entry per work session.\n\n"
        "THE STANDARD, in short:\n"
        "- Use your one unique, stable agent name in every heading; "
        "never a generic label, never another agent's name.\n"
        "- One independent, dated entry per session — never merged "
        "into vague summaries.\n"
        "- Each entry records: what was asked / what was done / how "
        "it was verified / decisions / end state / open items.\n"
        "- List ALL related files with machine + full path.\n"
        "- Record failures and corrections plainly.\n"
        "- NEVER write credential values anywhere — locations only.\n"
        "- File the entry in the same session as the work.\n\n"
        "HOW TO FILE (any one of these):\n"
        "- MCP tool: mesh_handover_log (title, asked, done, verified, "
        "decisions, files, open)\n"
        "- CLI: python3 -m agent_mesh.handover --title \"...\" "
        "--asked \"...\" --done \"...\" --verified \"...\" "
        "--files \"...\" --open \"...\"\n"
        "- Full rules: GET /handover/rules with your agent token.\n"
        "If you cannot file entries yourself, tell the owner what the "
        "entry should say — an unfiled session is a lost session.")
    return version, text
