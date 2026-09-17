# Audit notes (0.3.2 → 0.3.3 critical path)

## Root causes (Windows angry report)

1. **Foreign process on :8765** (Services session Python) returned HTTP 200 + `{"error":"Unknown endpoint"}` for `/`, `/health`, `/admin`. Not in Luxciax/grokbot2api sources. Tauri iframe + bare-200 health check treated it as “up”.
2. **`SandBackend.__init__` hard-required sbi_** → process exit → health timeout banner when Start allowed without renewal (0.3.2).

## Fixes in 0.3.3

- Deferred credential check; `/health` includes `service: grokbot2api`.
- Fingerprint health + auto port bump (+20) + `gateway.log`; default port **18765**.
- Do not report started unless fingerprint matches.

## Remaining / incomplete (non-blocking)

| Area | Severity | Note |
|------|----------|------|
| Credential import sbi_ | Med | Session JWT ≠ renewal; paste still required often |
| Image gen | Med | Needs session JWT path |
| Anthropic `/v1/messages` | Low | Present; needs live credential |
| Model catalogue | Low | Admin toggles OK |
| Sidecar CI packaging | Med | PyInstaller externalBin path fragile on Windows |
| Adopted orphan gateway | Low | Detected by fingerprint; Stop won't kill external PID |

## Verify

```bash
python grokbot2api.py --listen 127.0.0.1 --port 18765
curl -s http://127.0.0.1:18765/health   # service=grokbot2api
curl -s http://127.0.0.1:18765/admin | head
```
