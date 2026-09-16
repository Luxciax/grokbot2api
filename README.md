# grokbot2api

`grokbot2api` is a local compatibility proxy that lets [Grok Build](https://github.com/xai-org/grok-build) use Cursor-hosted Grok / Composer models through OpenAI-compatible Responses and Chat Completions endpoints.

It translates Grok Build requests into Cursor's undocumented `aiserver.v1.InferenceService.Stream` protobuf messages. Messages, native tool calls, tool results, multimodal image parts, and multi-turn state are preserved across the bridge.

> [!WARNING]
> This project uses an undocumented Cursor endpoint and private protobuf schema. It is not affiliated with, endorsed by, or supported by Cursor, Anysphere, xAI, or Grok Build. The integration can stop working without notice. Use only credentials issued to you and only in ways permitted by the applicable service terms.

## Features

- OpenAI-compatible `POST /v1/responses` and `POST /v1/chat/completions`
- Anthropic-compatible `POST /v1/messages` (and `/messages`) for Claude Code / Anthropic SDK basic chat + tools
- Native Cursor protobuf tool calls and tool results
- Vision / image input forwarding (`image_url`, `input_image`, Anthropic image blocks, data URLs, http(s) URLs)
- Image generation via `AiService/RunGenerateImage` (`POST /v1/images/generations`, catalogue `cursor-generate-image`)
- Multi-model catalogue for the Cursor Models pool (Grok 4.6 / 4.5, Composer 2.5, Fast variants)
- Multi-turn Grok Build agent loops
- Streaming SSE responses with heartbeats
- `GET /v1/models`, `GET /v1/usage`, richer `GET /health`, and `GET /docs`
- Local admin **workbench** at `GET /admin` (overview, models + custom aliases, keys, audits, media gallery, playground, settings)
- Bounded retry on upstream HTTP 429 / 502 / 503
- Optional CORS (`--cors-origins`)
- Automatic short-lived access-token renewal
- Loopback-only binding by default; Docker image binds `0.0.0.0` only with an API key
- No third-party Python runtime dependencies

## Requirements

- Linux (gateway) or Windows 10+ (desktop client + gateway)
- Python 3.10 or newer (when running from source)
- [Grok Build](https://github.com/xai-org/grok-build) installed
- Access to Cursor Models pool models such as `grok-4.6`, `grok-4.5`, or `composer-2.5`
- A valid `SAND_INFERENCE_RENEWAL_CREDENTIAL` issued to you

## Install

```bash
git clone https://github.com/taowen/grokbot2api.git
cd grokbot2api
chmod 700 grokbot2api.py sand_inference.py
```

The proxy uses only Python's standard library.

## Configure Grok Build

Copy entries from [`config.example.toml`](config.example.toml) into `~/.grok/config.toml`. Example for the full alias set:

```toml
[models]
default = "cursor-grok-4-6"

[model.cursor-grok-4-6]
name = "Cursor Grok 4.6 via grokbot2api"
model = "cursor-grok-4-6"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000

[model.cursor-grok-4-6-fast]
name = "Cursor Grok 4.6 Fast via grokbot2api"
model = "cursor-grok-4-6-fast"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000

[model.cursor-grok-4-5]
name = "Cursor Grok 4.5 via grokbot2api"
model = "cursor-grok-4-5"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000

[model.cursor-grok-4-5-fast]
name = "Cursor Grok 4.5 Fast via grokbot2api"
model = "cursor-grok-4-5-fast"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000

[model.cursor-composer-2-5]
name = "Cursor Composer 2.5 via grokbot2api"
model = "cursor-composer-2-5"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000

[model.cursor-composer-2-5-fast]
name = "Cursor Composer 2.5 Fast via grokbot2api"
model = "cursor-composer-2-5-fast"
base_url = "http://127.0.0.1:8765/v1"
api_backend = "responses"
api_key = "local-only"
context_window = 256000
```

Keep custom `model` values distinct from built-in IDs such as `grok-4.6`. Otherwise Grok Build can merge the custom endpoint with cached built-in metadata.

### Client alias → upstream mapping

| Client alias | Upstream `model_id` | Params |
|---|---|---|
| `cursor-grok-4-6` | `grok-4.6` | `effort=high`, `fast=false` |
| `cursor-grok-4-6-fast` | `grok-4.6` | `effort=high`, `fast=true` |
| `cursor-grok-4-5` | `grok-4.5` | `effort=high`, `fast=false` |
| `cursor-grok-4-5-fast` | `grok-4.5` | `effort=high`, `fast=true` |
| `cursor-composer-2-5` | `composer-2.5` | `fast=false` |
| `cursor-composer-2-5-fast` | `composer-2.5` | `fast=true` |

Bare upstream ids (`grok-4.6`, `grok-4.5`, `composer-2.5`) are also accepted. When the client omits `model`, the process `--model` value is used as the upstream id.

Verify that Grok Build sees the models:

```bash
grok models
```

### Context window

Cursor documents Cursor Grok models with a 256K context window on this route. System instructions, tool schemas, reasoning, conversation history, images, and tool results all consume part of that window.

## Start the proxy

Do not put the renewal credential directly in shell history. Read and export it interactively:

```bash
read -rsp "Cursor renewal credential: " SAND_INFERENCE_RENEWAL_CREDENTIAL
echo
export SAND_INFERENCE_RENEWAL_CREDENTIAL
./grokbot2api.py
```

Expected output:

```text
grokbot2api listening on http://127.0.0.1:8765/v1
admin: http://127.0.0.1:8765/admin
default alias: cursor-grok-4-6; fallback upstream: grok-4.6; models: …; upstream: …/sand_inference.py
```

Check the local endpoint:

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/v1/models
```

## Admin workbench

Open the loopback **完整工作台** UI (stdlib single-page HTML, dark theme, Chinese labels):

```text
http://127.0.0.1:8765/admin
```

(`/dashboard` is an alias.)

When an API key is configured (primary env key and/or admin-managed client keys), the workbench requires a valid Bearer / `x-api-key` (or the login page that stores the key in a cookie / `localStorage`). Auth is unchanged from earlier releases.

### Screenshots-in-words (sidebar tabs)

1. **总览** — Version, uptime, listen URL, enabled model count, request/success/error metrics, weekly `/v1/usage` card, recent errors, plus links to `/docs` and `/health`.
2. **模型** — Full catalogue table with enable/disable, set-default, badges for chat / vision / image-gen (and `custom`), upstream id + params. Form to **add a custom alias** (`alias`, `upstream_id`, params, capabilities) persisted via `POST /admin/api/models` into `admin_config.json`.
3. **密钥** — Create / revoke client API keys (preview only; never shows the primary env secret or Cursor credentials).
4. **审计** — Recent request audits table and JSON export (`GET /admin/api/audits`).
5. **媒体** — Gallery of `media/` index entries with thumbnails via `/media/<id>`, prompt/model/time, and delete (`GET/DELETE /admin/api/media…`).
6. **试用** — Chat playground (`POST /v1/chat/completions`) and image-gen form (`cursor-generate-image`, aspect ratio) that renders the result image.
7. **设置** — Read-only CORS origins, auth-required flag, docs/health links; **never shows secrets**.

Toggles, client keys, and custom model aliases persist to `admin_config.json` next to the proxy (path overridable with `--admin-config`).

## Use Grok Build

```bash
cd /path/to/your/project
grok -m cursor-grok-4-6
# or
grok -m cursor-composer-2-5-fast
```

Headless example:

```bash
grok -m cursor-grok-4-6 -p "Inspect this project and explain how it works."
```

## Vision / images

Chat Completions and Responses image parts are encoded as native `InferenceContentParts` (protobuf field 3) instead of the old omit placeholder. Supported shapes include:

- `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`
- `{"type":"input_image","image_url":"https://..."}}`
- AI SDK-style file/image parts with `media_type` starting with `image/`

Text-only messages still use field 2 for compatibility.

Vision **input** on the Grok / Composer catalogue models is supported.

## Image generation

Image generation uses a **second** Cursor RPC — not `InferenceService/Stream`:

```text
POST /aiserver.v1.AiService/RunGenerateImage
```

Auth is the renewal **session** token (same family as `DashboardService/GetSandUsageStatus`), with
sand client meta + `x-cursor-checksum`. The bridge exposes an OpenAI-compatible edge:

```bash
curl -s http://127.0.0.1:8765/v1/images/generations \
  -H "Authorization: Bearer $GROKBOT2API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cursor-generate-image",
    "prompt": "a watercolor fox under moonlight",
    "aspect_ratio": "1:1",
    "response_format": "url"
  }'
```

Aliases: `/images/generations`. Optional OpenAI `size` (e.g. `1024x1024`) maps to Cursor aspect
ratios `1:1` | `4:3` | `3:4` | `16:9` | `9:16`. Generated files are stored under `media/` and served
at `GET /media/<id>` (respects API key auth when configured). Catalogue entry:
`cursor-generate-image` (`capabilities: ["image_generation"]`). Client `model` is often ignored
server-side (Google image model).

## Command-line options

```text
--listen ADDRESS          Listen address (default: 127.0.0.1)
--port PORT               Listen port (default: 8765)
--model MODEL             Fallback upstream model when client omits model (default: grok-4.6)
--default-alias ALIAS     Default client-facing catalogue alias
--admin-config PATH       Persisted admin toggles JSON
--upstream-script PATH    Path to sand_inference.py
--backend-url URL         Override the Cursor backend URL
--cache PATH              Short-lived access-token cache
--max-mode                Enable max mode when the account supports it
--conversation-id ID      Override the upstream conversation ID
--timeout-ms MS           Upstream timeout (default: 600000)
--api-key-env NAME        Environment variable used to protect the local proxy
--cors-origins LIST       Comma-separated CORS origins (`*` allowed); empty disables CORS
```

Run `./grokbot2api.py --help` for the complete list.

## Protecting the local endpoint

The server binds to `127.0.0.1` by default. To require a bearer token even on loopback:

```bash
read -rsp "Local proxy API key: " GROKBOT2API_KEY
echo
export GROKBOT2API_KEY
./grokbot2api.py --api-key-env GROKBOT2API_KEY
```

Set the same value as `api_key` in the Grok Build model configuration.

You can also register additional client keys in the admin UI (`客户端 API Keys`) or via `POST /admin/api/keys`. Any registered key or the primary env key is accepted as `Authorization: Bearer …` or `x-api-key: …`.

The proxy refuses to bind to a non-loopback address unless the selected API-key environment variable is non-empty. Exposing this service to a network is strongly discouraged.

Optional CORS for browser clients:

```bash
./grokbot2api.py --cors-origins 'http://localhost:3000,http://127.0.0.1:3000'
# or allow any origin (less safe):
./grokbot2api.py --cors-origins '*'
```

## Anthropic Messages

Basic Claude Code / Anthropic SDK chat works against:

```bash
curl -s http://127.0.0.1:8765/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: YOUR_KEY' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"cursor-grok-4-6","max_tokens":512,"messages":[{"role":"user","content":"hi"}]}'
```

Streaming uses Anthropic SSE (`message_start` / `content_block_*` / `message_delta` / `message_stop`). Tool use / tool result blocks are mapped to the OpenAI tool-call path used by the sand backend. `/messages` is an alias of `/v1/messages`.

## Docker

```bash
cp .env.example .env   # set GROK_BUILD_PROXY_API_KEY and SAND_INFERENCE_RENEWAL_CREDENTIAL
docker compose up --build
```

Or build/run the image directly:

```bash
docker build -t grokbot2api:local .
docker run --rm -p 8765:8765 \
  -e GROK_BUILD_PROXY_API_KEY=change-me \
  -e SAND_INFERENCE_RENEWAL_CREDENTIAL="$SAND_INFERENCE_RENEWAL_CREDENTIAL" \
  -v grokbot2api-data:/data \
  grokbot2api:local
```

Admin config (models + client keys) persists under the `/data` volume as `admin_config.json`.

## Compared to [chenyme/grok2api](https://github.com/chenyme/grok2api)

| | **grokbot2api** (this repo) | **chenyme/grok2api** |
|---|---|---|
| Backend | Cursor sand `InferenceService.Stream` (Grok Bot / Models pool) | xAI / Grok web-style flows with account/token pools |
| Auth model | One Cursor renewal credential + optional local API keys | Multi-account / cookie / token pool management |
| Goal | Local OpenAI + Anthropic edge for Grok Build / Claude Code on Cursor-hosted models | Broader Grok reverse-proxy with load balancing across accounts |
| Runtime | Python stdlib only | Typically FastAPI / richer dependency stack |
| Not in scope | Account pools, cookie farms, CF bypass | Cursor sand protobuf bridge |

Feature ideas such as admin dashboards, multi-key auth, audits, CORS, Docker, and `/v1/messages` are inspired by that project's UX, but this proxy deliberately stays a **single-credential Cursor-sand adapter** — not an account pool.

## Architecture

```text
Grok Build / Claude Code / OpenAI SDK / Anthropic SDK
  OpenAI Responses | Chat Completions | Anthropic Messages + SSE
        |
        v
grokbot2api
  catalogue + multi keys + audits + message/tool/image bridge
        |
        v
Cursor api2 backend
  Connect protocol + private protobuf
        |
        v
Cursor-hosted Grok / Composer model
```

See [docs/protocol.md](docs/protocol.md) for the wire-format reference.

## Known limitations

- The Cursor inference API and protobuf schema are private and undocumented.
- `InferenceContentPart` image field numbers are a best-effort reconstruction (see protocol.md). Confirm with a live capture if vision requests fail.
- Grok models currently reject a present `InferenceAgentTool.parameters` protobuf field with provider status 422. The proxy omits that field and appends a compact argument signature to each native tool description.
- Upstream responses are buffered by the helper before they are converted to SSE. Heartbeats keep Grok Build's connection alive, but token deltas are not forwarded in real time.
- Image generation uses `AiService/RunGenerateImage` (session token), not `InferenceService/Stream`.
- Token usage may be reported as zero when the private endpoint omits usage frames.

## Troubleshooting

### `SAND_INFERENCE_RENEWAL_CREDENTIAL` is not set

Export a valid credential before starting the proxy. Never commit it, paste it into an issue, or include it in logs.

### Provider status 422

The upstream provider rejected a tool or model configuration. Confirm that you are running the latest proxy version, which omits the incompatible protobuf `parameters` field.

### Grok Build cannot find the model

Run `grok models` and validate `~/.grok/config.toml`. The table name, default model alias, and `-m` argument must match exactly.

## Security

Read [SECURITY.md](SECURITY.md) before deploying or modifying the proxy. In particular:

- Treat renewal credentials and cached access tokens as secrets.
- Keep the service on loopback.
- Rotate any credential pasted into a terminal transcript, chat, issue, or log.
- Do not publish request bodies; Grok Build may send source code and tool output.

## Windows desktop client

A tray / control-window app under [`windows/`](windows/) wraps the **local** gateway (default `127.0.0.1:8765`): start/stop, DPAPI-backed credential storage, optional proxy API key, and **Open Admin** for `/admin`.

- Run from source: see [`windows/README.md`](windows/README.md)
- **Pre-built EXE**: GitHub Actions (`.github/workflows/windows-release.yml`) builds on `windows-latest` for `v*` tags and `workflow_dispatch`
  - Portable: `grokbot2api-windows-portable-x64.zip`
  - Installer: `grokbot2api-windows-setup-x64.exe`
- Releases: <https://github.com/taowen/grokbot2api/releases>

The portable zip runs without installation. The installer targets Program Files with optional Start Menu / desktop shortcuts and an uninstaller.

## Development

The implementation is split by responsibility:

- `grokbot2api.py` contains the HTTP router, Chat Completions adapter, admin UI, audits, and CLI.
- `responses_api.py` contains Responses request conversion and SSE events.
- `messages_api.py` contains Anthropic Messages request/response conversion and SSE events.
- `api_common.py` contains shared content / image normalization.
- `image_gen.py` encodes/decodes `AiService/RunGenerateImage` and persists under `media/`.
- `model_catalogue.py` contains client aliases, client API keys, and admin persistence.
- `sand_inference.py` contains the credential exchange and Connect helpers.

Run the offline test suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile grokbot2api.py responses_api.py messages_api.py api_common.py model_catalogue.py sand_inference.py image_gen.py
python3 -m py_compile windows/app.py windows/main.py windows/credentials.py windows/gateway_service.py windows/tray_ui.py
```

No live credential is required for tests.

## License

[MIT](LICENSE)
