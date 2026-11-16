// Agent Mesh desktop shell (Tauri 2).
//
// What it does, and nothing more:
//   1. If a node is already serving on 127.0.0.1:8790 (for example the
//      systemd --user service on Susan's PC), the app is a pure window:
//      it spawns nothing and kills nothing on exit.
//   2. Otherwise, if ~/.agent-mesh-node/node.env exists, the app spawns
//      the Python node with exactly that configuration: every KEY=VALUE
//      pair from node.env is passed as environment (admin token, vault
//      master passphrase), and the existing data dir
//      (~/.agent-mesh-node/data, when present) is passed as --data-dir,
//      mirroring the installed service. No token is generated and no
//      token file is touched — the spawned process is the machine's own
//      node, not a blank rival.
//   3. Only on a machine with no node config at all does the app fall
//      back to generating an admin token: $AGENT_MESH_ADMIN if set,
//      else a fresh one written to
//      ~/.agent-mesh-node/desktop-admin-token.txt so it can be typed
//      into the GUI's login screen.
//   4. The window (tauri.conf.json) opens http://127.0.0.1:8790/.
//      On exit, only a node this process actually spawned is killed.
//
// The spawned node still needs python3 + the agent_mesh package on the
// machine (see DESKTOP.md); bundling a Python runtime is future work.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
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

fn port_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// Parse a KEY=VALUE env file (systemd EnvironmentFile style): blank
/// lines and #-comments are skipped, surrounding quotes are stripped.
fn parse_env_file(path: &Path) -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    let Ok(text) = fs::read_to_string(path) else {
        return pairs;
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        let mut value = value.trim();
        if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            value = &value[1..value.len() - 1];
        }
        if !key.is_empty() {
            pairs.push((key.to_string(), value.to_string()));
        }
    }
    pairs
}

/// The fresh-machine fallback: $AGENT_MESH_ADMIN if set, else a freshly
/// generated token, written to desktop-admin-token.txt so it can be
/// typed into the GUI's login screen. Only called when there is no
/// existing node configuration to reuse.
fn generated_admin_token(dir: &Path) -> String {
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
    let _ = fs::create_dir_all(dir);
    let _ = fs::write(dir.join("desktop-admin-token.txt"), &token);
    token
}

fn spawn_node() -> Child {
    let dir = home_dir().join(".agent-mesh-node");
    let env_file = dir.join("node.env");
    let mut cmd = Command::new("python3");
    cmd.args(["-m", "agent_mesh.node", "--port", "8790"]);
    if env_file.is_file() {
        // A configured node lives here: start THAT node, with its own
        // token, passphrase and data — never a blank rival.
        let pairs = parse_env_file(&env_file);
        let has_admin = pairs.iter().any(|(key, _)| key == "AGENT_MESH_ADMIN");
        for (key, value) in &pairs {
            cmd.env(key, value);
        }
        if !has_admin && std::env::var_os("AGENT_MESH_ADMIN").is_none() {
            let token = generated_admin_token(&dir);
            cmd.env("AGENT_MESH_ADMIN", token);
        }
        let data_dir = dir.join("data");
        if data_dir.is_dir() {
            cmd.arg("--data-dir").arg(data_dir);
        }
    } else {
        let token = generated_admin_token(&dir);
        cmd.env("AGENT_MESH_ADMIN", token);
    }
    cmd.spawn()
        .expect("failed to start the Agent Mesh node: is python3 installed?")
}

fn wait_for_port(port: u16, timeout: Duration) {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if port_open(port) {
            return;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
}

fn main() {
    // A node already serving on 8790 owns the mesh: be a pure window.
    let child = if port_open(8790) {
        None
    } else {
        let child = spawn_node();
        wait_for_port(8790, Duration::from_secs(15));
        Some(child)
    };

    let app = tauri::Builder::default()
        .manage(NodeProcess(Mutex::new(child)))
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
