# Changelog

## 0.3.3

- Fix: gateway starts without `SAND_INFERENCE_RENEWAL_CREDENTIAL` / sbi_ — `/health` and `/admin` work; inference fails later with a clear error.
- Fix: Tauri health probe requires grokbot2api JSON fingerprint (`service` / `ok`+`version`+`admin`); rejects foreign `{"error":"Unknown endpoint"}` on :8765.
- Fix: if configured port is occupied by a non-grokbot2api process, auto-pick next free port (up to +20) and persist settings.
- Fix: never report gateway started when fingerprint health fails; kill child and surface `gateway.log` tail.
- Fix: child stdout/stderr appended to local app data `gateway.log`.
- Change: default listen port **18765** (avoids common :8765 conflicts).
- CSP: allow iframe/connect to any `127.0.0.1` / `localhost` port.


## 0.3.2

- Fix: Start / 启动网关 is clickable without sbi_ renewal; missing renewal is a non-fatal banner + gateway `last_error` warning only.
- Fix: workbench sidebar sections (模型/密钥/审计/媒体/试用) show section-specific empty states when the gateway is down, so nav feels responsive.
- Keep: 总览 / 凭证 always work offline; admin `#hash` deep-links still remount the iframe when the gateway is up.
- Backend: `gateway.rs` `start()` no longer hard-fails on missing renewal; injects renewal env only when present.

## 0.3.1

- Fix: sidebar navigation no longer forced back to 凭证 when sbi_ / renewal is missing (poll no longer calls setNav).
- Fix: parse `cursor-accounts.accounts` as an object map keyed by account id (real Grok Bot shape); still supports legacy array.
- UX: banner + disabled Start when renewal missing; after saving sbi_ go to 工作台; clearer「本机未存储会话 JWT」messaging.
- Best-effort: also scan process env and a few more local JSON files for `sbi_` (never log secrets).

## v0.3.0 — 2026-09-17

### Added
- Tauri 2 Windows desktop client under `desktop/` (React + TypeScript)
  - Chinese sidebar: **总览** / **工作台** / **模型** / **密钥** / **审计** / **媒体** / **试用** / **凭证**
  - Embeds `/admin` in a webview; admin hash deep-links (`#models`, …)
  - Prefer PyInstaller sidecar `grokbot2api-server` (no system Python for portable builds); fall back to system Python + bundled scripts
  - DPAPI + Chromium OSCrypt import from `%APPDATA%\Grok Bot\` (+ `%USERPROFILE%\.grokbot\`)
  - Nested `cursor-accounts` token decrypt; best-effort `sbi_…` scan; paste if missing
  - Session JWT env (`SAND_SESSION_TOKEN`) for Dashboard / AiService image path
  - System tray; close hides to tray
- `windows/sidecar_main.py` + `grokbot2api-server.spec` for the headless sidecar
- `desktop/crates/os_crypt` + `tests/test_os_crypt_v10.py` / `tests/test_session_token_env.py`
- `docs/windows-credential-import.md`

### Changed
- Release assets: `grokbot2api-windows-setup-x64.exe`, `grokbot2api-windows-portable-x64.zip`
- Bump to **0.3.0**

### Deprecated
- Python/tk tray UI under `windows/` (sidecar build helpers remain)
