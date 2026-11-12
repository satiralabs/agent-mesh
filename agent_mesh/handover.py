import os
import time
import socket
import json
import subprocess
import shutil
from typing import Dict, Any, List, Optional

DEFAULT_MEMORY_DIR = os.path.expanduser("~/.agent-mesh/memory")
DEFAULT_PACK_DIR = os.path.expanduser("~/.agent-mesh/handover-pack")

def check_tcp_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def probe_local_services(probe_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for item in probe_list:
        name = item.get("name", "unknown")
        host = item.get("host", "127.0.0.1")
        port = item.get("port")
        if port:
            is_up = check_tcp_port(host, int(port))
            results.append({
                "name": name,
                "host": host,
                "port": port,
                "status": "UP (Listening)" if is_up else "DOWN"
            })
    return results

def read_file_safe(path: str) -> str:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

def compile_handover_pack(
    memory_dir: str = DEFAULT_MEMORY_DIR,
    out_dir: str = DEFAULT_PACK_DIR,
    probe_list: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    now_utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    today_str = time.strftime("%Y-%m-%d", time.gmtime())

    # Read base memory files if they exist
    memory_md = read_file_safe(os.path.join(memory_dir, "MEMORY.md"))
    user_md = read_file_safe(os.path.join(memory_dir, "USER.md"))
    agents_md = read_file_safe(os.path.join(memory_dir, "AGENTS.md"))
    tools_md = read_file_safe(os.path.join(memory_dir, "TOOLS.md"))
    today_log = read_file_safe(os.path.join(memory_dir, "memory", f"{today_str}.md"))

    # Live service check
    probe_results = probe_local_services(probe_list or [
        {"name": "Local Agent Broker", "port": 8088},
        {"name": "SpotGames Web Lobby", "port": 3020},
        {"name": "SpotGames Standalone Web", "port": 3030},
        {"name": "EverythingSub Reseller Bot", "port": 8095},
    ])

    service_table = "| Service | Host:Port | Live Verified Status |\n|---|---|---|\n"
    for s in probe_results:
        service_table += f"| {s['name']} | {s['host']}:{s['port']} | **{s['status']}** |\n"

    # 1. Generate 00-READ-ME-FIRST.md
    readme_content = f"""# READ ME FIRST — Handover for the Receiving Agent

Generated: {now_utc} · Auto-synchronized via agent-mesh.

You are an AI assistant taking over the user's workspace. This pack mirrors the continuous memory and state layout:

1. **Adopt `MEMORY.md`**: Long-term curated facts, preferences, and verified commitments.
2. **Review Verified Live State**: Check the service table in the handover file before assuming any service is running.
3. **Daily Work Logs (`memory/`)**: Review `{today_str}.md` for today's active work and decisions.
4. **Follow the Operating Rules**:
   - Provide exact copy-pasteable instructions directly in messages.
   - Verify live on the system before claiming work is finished.
   - Never spend wallet/store money without explicit, per-order user approval.
   - Never expose raw credentials into prompt or chat files.
"""
    readme_path = os.path.join(out_dir, "00-READ-ME-FIRST.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)

    # 2. Generate Handover-for-Another-LLM.md
    handover_content = f"""# HANDOVER — Distilled Operational Briefing

**Generated:** {now_utc} · **Compiled by:** agent-mesh standalone sync engine

---

## 1. Operating Rules & Boundaries

- **Exact instructions inside message:** The user does not work as a raw coder. Provide exact, paste-ready text and commands.
- **Verify live before claiming done:** Always check actual ports, services, and HTTP responses.
- **Strict money boundary:** No paid test is pre-approved. Every spend requires explicit user approval.
- **Zero-trust credential rule:** Never write secret tokens or private keys to chat, prompt, or memory files.

---

## 2. Live Verified Services (At Compilation Time)

{service_table}

---

## 3. Curated Long-Term Memory (MEMORY.md)

{memory_md if memory_md else "*(No MEMORY.md found in source memory directory)*"}

---

## 4. User Profile & Preferences (USER.md)

{user_md if user_md else "*(No USER.md found)*"}

---

## 5. Conventions & Operating Lessons (AGENTS.md)

{agents_md if agents_md else "*(No AGENTS.md found)*"}

---

## 6. Infrastructure & Connection Notes (TOOLS.md)

{tools_md if tools_md else "*(No TOOLS.md found)*"}

---

## 7. Active Day Log ({today_str}.md)

{today_log if today_log else "*(No log entry recorded yet for today)*"}
"""
    handover_path = os.path.join(out_dir, "Handover-for-Another-LLM.md")
    with open(handover_path, "w", encoding="utf-8") as f:
        f.write(handover_content)

    return {
        "readme": readme_path,
        "handover": handover_path
    }

def sync_handover_to_drive(pack_dir: str = DEFAULT_PACK_DIR, drive_folder_name: str = "Muse Handover") -> bool:
    readme_file = os.path.join(pack_dir, "00-READ-ME-FIRST.md")
    handover_file = os.path.join(pack_dir, "Handover-for-Another-LLM.md")

    if not os.path.exists(handover_file):
        return False

    # Check if gdrive CLI is available
    gdrive_bin = shutil.which("gdrive")
    if not gdrive_bin:
        # Fallback to local antigravity bin if present
        candidate = os.path.expanduser("~/.gemini/antigravity/bin/gdrive")
        if os.path.exists(candidate):
            gdrive_bin = candidate

    if not gdrive_bin:
        print("Warning: 'gdrive' CLI not found on PATH. Handover files saved locally only.")
        return False

    try:
        # Check if files already exist on Drive to choose between upload and update
        check_cmd = [gdrive_bin, "readonly", "search", "--name-exact", "Handover-for-Another-LLM.md", "--json"]
        proc = subprocess.run(check_cmd, capture_output=True, text=True, timeout=15)
        
        target_id = None
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                res = json.loads(proc.stdout)
                if isinstance(res, list) and len(res) > 0:
                    target_id = res[0].get("id")
            except Exception:
                pass

        if target_id:
            update_cmd = [gdrive_bin, "mutate", "update", target_id, handover_file]
            subprocess.run(update_cmd, check=True, timeout=30)
            print(f"Updated live handover on Drive (File ID: {target_id})")
        else:
            upload_cmd = [gdrive_bin, "mutate", "upload", handover_file]
            subprocess.run(upload_cmd, check=True, timeout=30)
            print("Uploaded new handover file to Drive")
        return True
    except Exception as e:
        print(f"Drive sync error: {e}")
        return False

def watch_and_sync(memory_dir: str = DEFAULT_MEMORY_DIR, out_dir: str = DEFAULT_PACK_DIR, interval: int = 300):
    print(f"Starting agent-mesh handover watcher. Syncing every {interval}s...")
    while True:
        try:
            pack = compile_handover_pack(memory_dir=memory_dir, out_dir=out_dir)
            print(f"[{time.strftime('%X')}] Compiled handover pack: {pack['handover']}")
            sync_handover_to_drive(pack_dir=out_dir)
        except Exception as e:
            print(f"Error during sync cycle: {e}")
        time.sleep(interval)
