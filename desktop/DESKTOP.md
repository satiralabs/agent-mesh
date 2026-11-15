# Agent Mesh — Desktop app (Tauri 2 scaffold)

**Status: SCAFFOLD ONLY — NOT COMPILED.** The machine that built Phase 3
has no Rust toolchain (`cargo: command not found`, checked 2026-11-15),
so nothing here has been built or run. The scaffold is complete and
ready to build on any machine with the prerequisites below; the commands
are exact, but treat the first build as unverified until it has run once.

## What it is

A thin Tauri 2 shell around the Phase 3 web GUI:

- `src-tauri/src/main.rs` spawns the Python node as a sidecar process
  (`python3 -m agent_mesh.node --port 8790`), waits for its port, and
  the window (configured in `src-tauri/tauri.conf.json`) opens the GUI
  at `http://127.0.0.1:8790/`. Closing the app kills the node.
- Admin token: if `AGENT_MESH_ADMIN` is set it is used as-is. Otherwise
  the shell generates one and writes it to
  `~/.agent-mesh-node/desktop-admin-token.txt` — open that file and type
  the token into the GUI's login screen.
- The vault also needs `AGENT_MESH_MASTER` in the environment (same as
  running the node by hand). Bundling a Python runtime and an OS-keystore
  master key is Phase 5 packaging work, not done here.

## Prerequisites (all platforms)

- Rust: install via https://rustup.rs
- Tauri CLI: `cargo install tauri-cli --version "^2"`
- Python 3.11+ on PATH, with Agent Mesh installed:
  from the repo root, `pip install .` (or run with
  `PYTHONPATH=<repo root>` in the environment)
- One 1024×1024 PNG for the app icon, then once, from `desktop/`:
  `cargo tauri icon path/to/icon.png` (generates `src-tauri/icons/`)

Platform extras:

- **Windows:** Microsoft C++ Build Tools (MSVC) + WebView2 runtime
  (pre-installed on Windows 11; the installer can add it on Windows 10).
- **Linux (Debian/Ubuntu):**
  `sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev`
- **macOS:** Xcode Command Line Tools (`xcode-select --install`).

## Build commands

Run from the `desktop/` folder:

```bash
cargo tauri dev     # development run (hot window on the live node)
cargo tauri build   # release bundles
```

Bundle targets are configured in `tauri.conf.json` for all three OSes;
Tauri builds only the targets valid on the machine you run it on:

- Windows → `.msi` and NSIS `.exe` under `src-tauri/target/release/bundle/`
- Linux → `.deb` and `.AppImage` under the same folder
- macOS → `.app` and `.dmg` under the same folder

So producing all installers takes one build machine per OS (or CI) —
there is no cross-compiling the bundles from a single OS.
