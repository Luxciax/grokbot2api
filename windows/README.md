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

GitHub Actions workflow [`.github/workflows/windows-release.yml`](../.github/workflows/windows-release.yml) runs on `windows-latest` when you push a `v*` tag or trigger `workflow_dispatch`.

Artifacts / release assets:

| Asset | Description |
| --- | --- |
| `grokbot2api-windows-portable-x64.zip` | Unzip and run `grokbot2api.exe` (no install) |
| `grokbot2api-windows-setup-x64.exe` | Inno Setup installer (Program Files, Start Menu / desktop shortcuts, uninstaller, optional launch after install) |

The EXE is **only** built on Windows runners. Linux/macOS checkouts can still import the `windows/` modules for offline unit tests (DPAPI paths are mocked / skipped).

### Local packaging (Windows machine)

```powershell
powershell -File windows\build_portable.ps1
powershell -File windows\build_installer.ps1   # needs Inno Setup 6
```

Build-only deps: `windows/requirements-build.txt` (PyInstaller, pystray, Pillow).

## Version

Client `__version__` defaults to `0.2.0` and is kept in sync with `grokbot2api.__version__`.
