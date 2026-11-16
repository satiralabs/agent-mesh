# Agent Mesh — Desktop app (Tauri 2)

**Status: BUILT AND RELEASED.** The desktop app is compiled by GitHub
Actions (`.github/workflows/desktop-release.yml`, manual trigger) on
Linux, Windows and macOS runners, and the installers are attached to
the GitHub Release:

**https://github.com/satiralabs/agent-mesh/releases/tag/v2.2.2**

- Windows: `Agent.Mesh_2.2.2_x64_en-US.msi` or
  `Agent.Mesh_2.2.2_x64-setup.exe` (NSIS)
- macOS: `Agent.Mesh_2.2.2_universal.dmg` (universal — Apple Silicon
  and Intel)
- Linux: `Agent.Mesh_2.2.2_amd64.AppImage` or
  `Agent.Mesh_2.2.2_amd64.deb`

The binaries are **unsigned** (no code-signing certificate), so each OS
shows a warning on first launch:

- **Windows:** SmartScreen — click "More info" → "Run anyway".
- **macOS:** Gatekeeper — right-click the app → "Open" (the first
  time; afterwards it opens normally).
- **Linux:** for the AppImage, `chmod +x Agent.Mesh_*.AppImage` once,
  then run it.

## What it does

A thin Tauri 2 shell around the web GUI. The window opens
`http://127.0.0.1:8790/`. What happens behind it depends on the machine:

1. **A node is already serving on port 8790** (for example the
   systemd `--user` service from `installers/install-linux.sh`): the
   app is a **pure window**. It spawns nothing and kills nothing on
   exit — your node, its data and its tokens are untouched.
2. **No node is serving, but `~/.agent-mesh-node/node.env` exists:**
   the app spawns the Python node with exactly that configuration —
   every `KEY=VALUE` pair from `node.env` is passed as environment
   (admin token, vault master passphrase) and the existing data dir
   (`~/.agent-mesh-node/data`, when present) is passed as `--data-dir`,
   mirroring the installed service. No token is generated and no token
   file is touched: the app starts *your* node, never a blank rival.
   Closing the app kills only the node it spawned.
3. **No node config at all** (fresh machine): the app generates an
   admin token, writes it to
   `~/.agent-mesh-node/desktop-admin-token.txt` (type it into the
   GUI's login screen), and spawns the node with it. If
   `AGENT_MESH_ADMIN` is set in the environment it is used as-is.

## Honest remainder

**Python runtime bundling is NOT done.** In the spawn cases (2 and 3)
the app still needs a system Python 3.11+ on PATH with Agent Mesh
importable (`pip install .` from the repo root, or `PYTHONPATH`
pointing at it), and the vault master passphrase comes from
`node.env` / the environment — there is no bundled interpreter and no
OS-keystore integration yet. On a machine already running a node
(case 1) none of that matters: the app is just a window.

## App icon

`src-tauri/icons/` holds the icon set wired into `tauri.conf.json`
(`bundle.icon`): `32x32.png`, `128x128.png`, `128x128@2x.png` (256),
`icon.png` (512) and a multi-size `icon.ico`, plus `icon.svg` as the
editable source. The mark — a hub node linked to five satellite nodes
in the v1 palette — is drawn by `desktop/make-icon.py` (Pillow). The
macOS `icon.icns` is not checked in; CI generates the full set before
every build with `tauri icon desktop/src-tauri/icons/icon.png`.

## Building from source

Prerequisites:

- Rust: install via https://rustup.rs
- Tauri CLI: `cargo install tauri-cli --version "^2"`
- Python 3.11+ on PATH, with Agent Mesh installed (see above)
- Platform extras:
  - **Windows:** Microsoft C++ Build Tools (MSVC) + WebView2 runtime
    (pre-installed on Windows 11; the installer can add it on
    Windows 10).
  - **Linux (Debian/Ubuntu):**
    `sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf`
  - **macOS:** Xcode Command Line Tools (`xcode-select --install`).

Run from the `desktop/` folder:

```bash
cargo tauri dev     # development run
cargo tauri build   # release bundles for the OS you are on
```

Bundle targets are configured in `tauri.conf.json` for all three OSes;
Tauri builds only the targets valid on the machine you run it on, so
producing all installers takes one build machine per OS — which is
what the GitHub Actions workflow does.
