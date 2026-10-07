# Changelog

## 0.3.9

- Add: Claude Opus 5.5 catalogue family from `AiService/GetUsableModels` packed ids (`claude-opus-5-5-high` / `-high-fast` / `-medium` / `-low` / `-max` / `-xhigh` and fast variants).
- Add curated aliases: `cursor-claude-opus-5-5` (+ effort/fast variants), plus short `claude-opus-5-5`, `opus-5.5`, `opus-5-5` (default map to high).
- Note: listing an id does not guarantee inference. Agent mode (`GrokBotService`) may still auto-pick the sand model; Stream (`InferenceService/Stream`) needs a valid `sbi_`/session credentials and account permissions.
- Keep existing Grok / Composer / image catalogue entries unchanged.

## 0.3.8

- Fix: in `chat_mode=agent` (default), ignore client `tools` and stay on GrokBotService instead of falling through to `InferenceService/Stream` (Hermes and similar clients attach builtin tools → previous 502 `ERROR_NOT_HIGH_ENOUGH_PERMISSIONS`).
- Add: `GROKBOT_AGENT_STRIP_TOOLS=0/false` restores Stream-when-tools for accounts that have Stream access.
- Improve: shorter Chinese message for `ERROR_NOT_HIGH_ENOUGH_PERMISSIONS` (mentions client tools as a common cause).

## 0.3.7

- Fix: bridge `/v1/chat/completions` through official **GrokBotService** (`SendGrokBotUserMessage` + transcript poll) using the session JWT — same path as Grok Bot 0.58 desktop. `InferenceService/Stream` remains via `GROKBOT_CHAT_MODE=stream` but still returns `ERROR_NOT_HIGH_ENOUGH_PERMISSIONS` on SuperGrok + Cursor Free.
- Fix: persist renewal `sessionToken` in the token cache (needed by GrokBotService / Dashboard / image gen).
- Note: GrokBotService selects model inside the sand agent (not per OpenAI `model`); client model id is echoed. Pin with `GROKBOT_AGENT_ID` or auto find/create name `grokbot2api`.
- Add: `grokbot_chat.py`; CLI `--chat-mode agent|stream`, `--grokbot-agent-id`, `--grokbot-agent-name`.

## 0.3.6

- Align client headers with Grok Bot 0.58: version `0.58.0`, `x-cursor-client-source: sand-desktop`, `x-cursor-client-os: CLIENT_OS_*`.
- Catalogue: prefer packed upstream ids (`grok-4.7-high`, `*-high-fast`, `composer-2.5`) and set RequestedModel field 8 (`is_variant_string_representation`) when params are empty; correct built_in/variant field numbers (7/8).
- Improve `ERROR_NOT_HIGH_ENOUGH_PERMISSIONS` message: do not blame expired credentials; note official chat uses `SendGrokBotUserMessage` while Stream stays denied on some SuperGrok+Cursor Free accounts (`noUsageBasedAllowed=true`) even after header/model packing.

## 0.3.5

- Fix: workbench/admin never displays `[object Object]` — shared `formatErr` extracts `error.message` / JSON.
- Fix: API error responses include a flat top-level `message` string alongside OpenAI-shaped `error` for stubborn Chinese clients.
- Fix: upstream dict errors (e.g. permission_denied trailer) coerced to a readable string before SSE/JSON.
- Fix: creating a client API key with an empty field auto-generates `gb_…`; clearer Chinese validation/auth errors.
- Fix: after first key create, workbench stores the new key in localStorage so admin APIs keep working; `/admin` HTML no longer depends on HttpOnly cookies (Tauri iframe); login at `/admin/login`.
- Fix: catch Windows `ConnectionAbortedError` when writing JSON responses.

## 0.3.4

- Add Grok 4.7 catalogue aliases: `cursor-grok-4-7` / `cursor-grok-4-7-fast` (upstream `grok-4.7`, effort=high, fast false/true) plus bare `grok-4.7` passthrough.
- Change default alias to `cursor-grok-4-7` and fallback upstream to `grok-4.7`.
- Docs/config: README table, `config.example.toml`, and protocol notes updated for 4.7; keep 4.6 / 4.5 / Composer 2.5.
- Research note: image gen remains `AiService/RunGenerateImage` (`cursor-generate-image`); no new selectable image model id; no video-generation RPC in Grok Bot / AiService.

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

