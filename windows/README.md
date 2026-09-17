> [!WARNING]
> **已弃用（Deprecated）**：请改用仓库根目录下的 [`desktop/`](../desktop/) Tauri 2 客户端（内嵌 `/admin` 工作台）。
> 本目录 Tk/`pystray` 客户端仍保留以便对照与紧急回退，但不再作为主发布通道。

# Windows desktop client

Small tray / control-window UI that wraps the **local** `grokbot2api` HTTP gateway (default `http://127.0.0.1:8765`). It does not talk to a cloud site; it starts the same stdlib Python proxy bundled from this repository.

## Features

- Start / stop the local gateway
- Store `SAND_INFERENCE_RENEWAL_CREDENTIAL` securely (DPAPI via `ctypes` when available; otherwise an obfuscated file under `%APPDATA%\grokbot2api`)
- Optional local proxy API key (`GROK_BUILD_PROXY_API_KEY`)
- Open the admin workbench (`/admin`) in your default browser
- System tray menu when `pystray` + Pillow are available; otherwise a small Tk control window
- Writable state (token cache, `admin_config.json`, generated media) under `%LOCALAPPDATA%\grokbot2api`

Secrets are never written to logs.

## Run from source (developer)

Requirements: Python 3.10+ on Windows, with the repo root as the working tree. Tkinter is part of the standard Windows CPython installer.

```powershell
cd path\to\grokbot2api
# optional tray extras:
python -m pip install -r windows\requirements.txt
python -m windows.app
# or:
python windows\main.py
```

Set the renewal credential via **Set Credential** in the UI (preferred) or in the environment before launch.

## What CI produces

Primary Windows artifacts now come from the **Tauri** client in [`desktop/`](../desktop/) via
[`.github/workflows/windows-release.yml`](../.github/workflows/windows-release.yml) (`windows-latest`, `v*` / `workflow_dispatch`):

| Asset | Description |
| --- | --- |
| NSIS `.exe` / MSI | Tauri installers |
| `grokbot2api-windows-portable-x64.zip` | Tauri portable (system Python 3.10+ required) |

This legacy Tk tree may still be built as a **non-blocking** optional job. Prefer `desktop/`.

### Local packaging (Windows machine)

```powershell
powershell -File windows\build_portable.ps1
powershell -File windows\build_installer.ps1   # needs Inno Setup 6
```

Build-only deps: `windows/requirements-build.txt` (PyInstaller, pystray, Pillow).

## Version

Client `__version__` defaults to `0.3.0` and is kept in sync with `grokbot2api.__version__`.
