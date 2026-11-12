import sys
import os
import time
import socket
import threading
import subprocess
import shutil

def is_server_running(port: int = 8088) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except Exception:
        return False

def ensure_server(port: int = 8088):
    if not is_server_running(port):
        from .broker import run_broker
        t = threading.Thread(target=run_broker, kwargs={"port": port}, daemon=True)
        t.start()
        for _ in range(20):
            if is_server_running(port):
                break
            time.sleep(0.1)

def launch_native_gui(port: int = 8088):
    ensure_server(port)
    url = f"http://127.0.0.1:{port}/"

    # Launch standalone dedicated app window via Chrome/Brave/Chromium
    browsers = [
        "google-chrome-stable",
        "/usr/bin/google-chrome-stable",
        "google-chrome",
        "brave",
        "chromium"
    ]

    for b in browsers:
        b_path = shutil.which(b) or (b if os.path.exists(b) else None)
        if b_path:
            cmd = [b_path, f"--app={url}"]
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return 0
            except Exception:
                continue

    # Fallback to default browser
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0
