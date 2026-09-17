# Windows credential import (Grok Bot → grokbot2api)

This document describes how the Tauri Windows client (`desktop/`) imports credentials from a local
Grok Bot install. **Never paste real secrets into git, issues, or chat.**

## Install locations

| Role | Path |
|------|------|
| App install | `%LOCALAPPDATA%\Programs\Grok Bot\` |
| User data | `%APPDATA%\Grok Bot\` |

## A) Always importable from Windows (DPAPI + Chromium OSCrypt)

**File:** `%APPDATA%\Grok Bot\sand-secrets.json`

Relevant keys:

- `cursor-machine-id` — safeStorage `v10` blob → UUID (registered machine id)
- `local-exec-file-key` — safeStorage → base64 of a 32-byte AES key (local-exec sealed files)
- `cursor-accounts` — plaintext JSON string shaped as
  `{ "active": "<64-hex>", "accounts": { "<64-hex>": { … } } }`
  (`accounts` is an **object map**, not an array; legacy array still accepted).
  Per-account `cursor-access-token` / `cursor-refresh-token` values are safeStorage base64 (`djEw…` / v10)

### Decrypt path

1. Read `%APPDATA%\Grok Bot\Local State` → `os_crypt.encrypted_key` (base64, magic prefix `DPAPI`)
2. `CryptUnprotectData(encrypted_key[5:])` → 32-byte AES key
3. For each `v10` blob: `nonce = buf[3:15]`, AES-GCM decrypt (auth tag at end)

In the desktop client open **设置** → **导入 Grok Bot 凭证**. The client stores:

- Machine id → `SAND_MACHINE_ID` / secure store
- Cursor access / refresh tokens (best-effort from `cursor-accounts`) for session-style / usage features

### Important: Cursor JWTs are not the inference renewal credential

Posting a Cursor access / refresh JWT to:

`POST https://api2.cursor.sh/sand-box/inference-credential`

returns **HTTP 401**. Those JWTs are **not** `SAND_INFERENCE_RENEWAL_CREDENTIAL`.

## B) Inference renewal credential (`SAND_INFERENCE_RENEWAL_CREDENTIAL`)

- Format: prefix `sbi_`, length ~47
- Lives in the **Grok Bot sand box** process environment as `SAND_INFERENCE_RENEWAL_CREDENTIAL`
  (provisioned into the box; **not** stored in Windows `sand-secrets.json`)
- Renewing with this credential against `/sand-box/inference-credential` returns 200 + a short-lived accessToken

Local-exec sealed files under `%USERPROFILE%\.grokbot\` (`sandSealedFile:1`) decrypt with the
file key (AES-256-GCM: `nonce12 | tag16 | ciphertext`). Contents are `slx_…` local-exec
credentials, **not** `sbi_`.

### Import UX (desktop/)

1. **导入 Grok Bot 凭证** — OSCrypt path above (machine id + session JWTs)
2. **粘贴推理续期凭证（sbi_…）** — required for inference; stored via Windows Credential Manager / DPAPI
3. Optional local proxy API key (`GROK_BUILD_PROXY_API_KEY`)

UI redacts secrets. Logs never print token values. **Session JWT ≠ inference renewal.**

## Secure storage

The Tauri client uses the Windows Credential Manager (with a non-Windows obfuscated-file fallback
for development) and injects values into the gateway child process environment:

| Env | Purpose |
|-----|---------|
| `SAND_INFERENCE_RENEWAL_CREDENTIAL` | `sbi_…` renewal |
| `SAND_MACHINE_ID` | stable device id |
| `GROK_BUILD_PROXY_API_KEY` | optional local proxy Bearer |

## Related

- Gateway: `sand_inference.py`, `grokbot2api.py`
- Tauri client: `desktop/`
- Legacy tray (deprecated): `windows/`


## B) Best-effort renewal discovery

Scans known Grok Bot / `.grokbot` files for `sbi_…`. Often empty — paste remains supported.
Secrets stay in the OS credential store; the UI never receives plaintext tokens.
