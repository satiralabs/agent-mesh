// Agent Mesh desktop shell (Tauri 2).
//
// What it does, and nothing more:
//   1. Works out the node admin token: $AGENT_MESH_ADMIN if set, else a
//      freshly generated one, written to
//      ~/.agent-mesh-node/desktop-admin-token.txt so Susan can type it
//      into the GUI's login screen.
//   2. Spawns the Python node as a sidecar process:
//      `python3 -m agent_mesh.node --port 8790`
//      (python3 + the agent_mesh package must be installed — see
//      DESKTOP.md. AGENT_MESH_MASTER is inherited from the environment
//      for the vault; bundling a Python runtime is a Phase 5 job.)
//   3. Waits for the node port, then opens the window configured in
//      tauri.conf.json at http://127.0.0.1:8790/ (the Phase 3 GUI).
//   4. Kills the node process when the app exits.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;

struct NodeProcess(Mutex<Option<Child>>);

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn admin_token() -> String {
    if let Ok(tok) = std::env::var("AGENT_MESH_ADMIN") {
        if !tok.is_empty() {
            return tok;
        }
    }
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let token = format!("{:x}{:x}", nanos, std::process::id());
    let dir = home_dir().join(".agent-mesh-node");
    let _ = fs::create_dir_all(&dir);
    let _ = fs::write(dir.join("desktop-admin-token.txt"), &token);
    token
}

fn wait_for_port(port: u16, timeout: Duration) {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
}

fn main() {
    let token = admin_token();
    let child = Command::new("python3")
        .args(["-m", "agent_mesh.node", "--port", "8790"])
        .env("AGENT_MESH_ADMIN", &token)
        .spawn()
        .expect("failed to start the Agent Mesh node: is python3 installed?");
    wait_for_port(8790, Duration::from_secs(15));

    let app = tauri::Builder::default()
        .manage(NodeProcess(Mutex::new(Some(child))))
        .build(tauri::generate_context!())
        .expect("error while building the Agent Mesh app");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<NodeProcess>() {
                if let Ok(mut slot) = state.0.lock() {
                    if let Some(mut child) = slot.take() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        }
    });
}
