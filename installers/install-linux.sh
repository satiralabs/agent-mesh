#!/usr/bin/env bash
# Agent Mesh — Linux installer (v2.1).
#
# Installs a per-user mesh node exactly the way the reference setup
# runs it: a systemd --user service (enabled, survives reboot via
# lingering) plus a desktop launcher for the GUI. NO sudo anywhere —
# everything lives in your home directory.
#
# Idempotent: re-running keeps your existing admin token / vault
# passphrase and data; it only rewrites the unit + launcher and
# restarts the service.
#
# Usage:  ./install-linux.sh [--port 8790] [--name "My Node"]
set -euo pipefail

PORT=8790
NODE_NAME="$(hostname)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --name) NODE_NAME="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$HOME/.agent-mesh-node"
ENV_FILE="$DATA_DIR/node.env"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/agent-mesh-node.service"
DESKTOP_FILE="$HOME/.local/share/applications/agent-mesh.desktop"

command -v python3 >/dev/null || { echo "python3 not found — install Python 3.11+ first." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl not found — this installer needs systemd --user." >&2; exit 1; }

echo "Agent Mesh Linux installer"
echo "  repo:  $REPO_DIR"
echo "  data:  $DATA_DIR"
echo "  port:  $PORT   node name: $NODE_NAME"

mkdir -p "$DATA_DIR/data" "$UNIT_DIR" "$(dirname "$DESKTOP_FILE")"
chmod 700 "$DATA_DIR"

# 1. Secrets env file (0600). Created once; never overwritten, so the
#    vault passphrase can never change under an existing vault.
if [[ ! -f "$ENV_FILE" ]]; then
  ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  MASTER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  cat > "$ENV_FILE" <<EOF
# Agent Mesh node secrets — keep this file 0600, never share it.
AGENT_MESH_ADMIN=$ADMIN_TOKEN
AGENT_MESH_MASTER=$MASTER
EOF
  echo "  created $ENV_FILE with a fresh admin token + vault passphrase (0600)"
else
  echo "  kept existing $ENV_FILE (tokens unchanged)"
fi
chmod 600 "$ENV_FILE"

# 2. systemd --user unit.
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Agent Mesh v2 Node
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONPATH=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 -m agent_mesh.node --port $PORT --name "$NODE_NAME" --data-dir $DATA_DIR/data
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
echo "  wrote $UNIT_FILE"

# 3. Desktop launcher for the GUI.
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Agent Mesh
Comment=Agent Mesh control centre
Exec=xdg-open http://127.0.0.1:$PORT/
Icon=$REPO_DIR/desktop/src-tauri/icons/icon.png
Terminal=false
Categories=Development;Utility;
EOF
echo "  wrote $DESKTOP_FILE"

# 4. Enable + (re)start. Lingering lets the service start at boot
#    before login; enabling it for your user may print a hint if
#    lingering is off — the command below is user-level, no sudo,
#    but on some systems lingering itself needs one sudo call by you:
#      sudo loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable agent-mesh-node.service >/dev/null
systemctl --user restart agent-mesh-node.service
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "  note: to start the node at boot, run once: sudo loginctl enable-linger $USER"

sleep 1
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "  node is UP: http://127.0.0.1:$PORT/"
else
  echo "  node did not answer yet — check: systemctl --user status agent-mesh-node"
fi

echo
echo "Done. Open the GUI at http://127.0.0.1:$PORT/ and log in with the"
echo "AGENT_MESH_ADMIN value from $ENV_FILE."
echo "Stop it any time with: systemctl --user stop agent-mesh-node"
