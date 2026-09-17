# grokbot2api 0.3.3 audit (2026-09-17)

## Critical (fixed this round)

1. **Port 8765 hijack** — Another Python in Windows *Services* session answered `:8765` with `{"error":"Unknown endpoint"}`. Tauri health only checked HTTP 200, so workbench loaded the wrong process. **Fix:** health fingerprint (`"ok"`+`"version"`); auto-pick next free port.
2. **Python hard-required `SAND_INFERENCE_RENEWAL_CREDENTIAL`** — Process exited immediately → health timeout. **Fix:** warn on stderr; serve `/health`+`/admin` without sbi_; inference fails later with a clear error.

## High (still open)

3. **sbi_ not auto-importable** — Only in remote sand-box agent env; Windows files/JWTs cannot substitute.
4. **Workbench is a thin iframe around `/admin`** — Deep-link hashes depend on admin `applyHashRoute`; UX still rough vs a native shell.
5. **Token cache / renewal exchange** — Without valid sbi_, chat/inference endpoints cannot succeed (by design).

## Medium

6. Model catalogue / custom upstream / provider pools — incomplete vs full grok2api-style product.
7. Image gen depends on session JWT path; not validated end-to-end in this audit.
8. Legacy Tk `windows/` client deprecated but still built in CI.
9. Admin auth optional when no API key — OK for localhost, weak if ever exposed.

## Low / polish

10. Chinese mojibake risk in some release notes / comments on Windows checkouts.
11. Node 20 deprecation warnings in GitHub Actions.

## Verify

```bat
python grokbot2api.py --listen 127.0.0.1 --port 18765
```

Then open `http://127.0.0.1:18765/admin` and `/health`. In Tauri client, Start should skip occupied 8765 and show the chosen port in the status bar.
