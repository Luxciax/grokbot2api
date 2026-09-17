# Changelog

## v0.3.0 — 2026-09-17

### Added
- Tauri 2 Windows desktop client under `desktop/` (React + TypeScript)
  - Chinese sidebar shell: **总览** / **工作台** / **设置**
  - Embeds `/admin` workbench (`http://127.0.0.1:8765/admin`) in a webview
  - Start/stop local Python gateway
  - DPAPI + Chromium OSCrypt import from `%APPDATA%\Grok Bot\sand-secrets.json`
  - Explicit paste for `SAND_INFERENCE_RENEWAL_CREDENTIAL` (`sbi_…`); session JWT ≠ inference renewal
  - System tray (open / start / stop / quit); close hides to tray
- `desktop/crates/os_crypt` AES-GCM v10 unit tests
- `docs/windows-credential-import.md` documenting A/B credential paths
- `tests/test_os_crypt_v10.py` cross-check for the decrypt shape

### Changed
- Windows release workflow (`.github/workflows/windows-release.yml`) builds Tauri NSIS/MSI + portable zip on `windows-latest` for `v*` tags
- Bump project / client version to **0.3.0**

### Deprecated
- Python/tk/PyInstaller tray client under `windows/` (kept for reference / emergency fallback)

### Known gaps
- Auto-import of `SAND_INFERENCE_RENEWAL_CREDENTIAL` from a connected sand box is not wired; paste is the supported path
