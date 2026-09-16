"""Cursor AiService/RunGenerateImage client (OpenAI images bridge helper).

Image generation is a separate unary RPC from ``InferenceService/Stream``.
Auth uses the renewal **session** token (``accessToken`` type session), the same
family as ``DashboardService/GetSandUsageStatus``, plus sand client meta and
``x-cursor-checksum``.

Protobuf (from Cursor runtime ``aiserver.v1``):

  message GenerateImageReferenceImage {
    string data = 1;       // base64 image bytes
    string mime_type = 2;
  }
  message RunGenerateImageRequest {
    string description = 1;
    repeated GenerateImageReferenceImage reference_images = 2;
    string model_id = 3;
    bool max_mode = 4;
    optional string aspect_ratio = 5;  // "1:1"|"4:3"|"3:4"|"16:9"|"9:16"
  }
  message RunGenerateImageSuccess {
    string image_data = 1;  // base64
    string mime_type = 2;
  }
  message RunGenerateImageError {
    string error = 1;
    bool model_restricted = 2;
    optional int32 provider_status_code = 3;
    bool content_safety_blocked = 4;
  }
  message RunGenerateImageResponse {
    oneof result {
      RunGenerateImageSuccess success = 1;
      RunGenerateImageError error = 2;
    }
  }

Connect unary JSON uses camelCase field names. The default transport posts JSON
(like GetSandUsageStatus); protobuf encode/decode helpers are available for
connect+proto and offline tests. Injectable ``transport`` callables keep unit
tests offline.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import sand_inference as upstream

GENERATE_IMAGE_PATH = "/aiserver.v1.AiService/RunGenerateImage"
DEFAULT_MEDIA_DIR = Path(__file__).with_name("media")
INDEX_NAME = "index.json"

ASPECT_RATIOS = frozenset({"1:1", "4:3", "3:4", "16:9", "9:16"})

# OpenAI size strings → closest supported Cursor aspect ratio.
SIZE_TO_ASPECT: dict[str, str] = {
    "256x256": "1:1",
    "512x512": "1:1",
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1536x1024": "16:9",
    "1024x1536": "9:16",
    "1344x768": "16:9",
    "768x1344": "9:16",
    "1152x896": "4:3",
    "896x1152": "3:4",
}

TransportFn = Callable[[str, bytes, dict[str, str]], tuple[int, bytes, str]]


class ImageGenError(RuntimeError):
    """Upstream or local image-generation failure."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        model_restricted: bool = False,
        content_safety_blocked: bool = False,
        provider_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.model_restricted = model_restricted
        self.content_safety_blocked = content_safety_blocked
        self.provider_status_code = provider_status_code


def normalize_aspect_ratio(value: str | None, size: str | None = None) -> str | None:
    """Return a supported aspect ratio string, or None when unspecified."""
    if isinstance(value, str) and value.strip():
        ratio = value.strip()
        if ratio not in ASPECT_RATIOS:
            raise ValueError(
                f"aspect_ratio must be one of {sorted(ASPECT_RATIOS)}; got {ratio!r}"
            )
        return ratio
    if isinstance(size, str) and size.strip():
        mapped = SIZE_TO_ASPECT.get(size.strip().lower())
        if mapped:
            return mapped
        # Infer from WxH when possible.
        if "x" in size.lower():
            try:
                width_s, height_s = size.lower().split("x", 1)
                width, height = int(width_s), int(height_s)
            except ValueError:
                return None
            if width == height:
                return "1:1"
            if width > height:
                return "16:9" if width / height >= 1.5 else "4:3"
            return "9:16" if height / width >= 1.5 else "3:4"
    return None


def encode_reference_image(data_b64: str, mime_type: str = "image/png") -> bytes:
    body = upstream.pb_str(1, data_b64)
    if mime_type:
        body += upstream.pb_str(2, mime_type)
    return body


def encode_generate_image_request(
    description: str,
    *,
    reference_images: list[dict[str, str]] | None = None,
    model_id: str = "",
    max_mode: bool = False,
    aspect_ratio: str | None = None,
) -> bytes:
    """Encode ``RunGenerateImageRequest`` protobuf bytes."""
    if not description or not str(description).strip():
        raise ValueError("description/prompt is required")
    body = upstream.pb_str(1, str(description))
    for image in reference_images or []:
        data = str(image.get("data") or "")
        if not data:
            continue
        mime = str(image.get("mime_type") or image.get("mimeType") or "image/png")
        body += upstream.pb_msg(2, encode_reference_image(data, mime))
    if model_id:
        body += upstream.pb_str(3, model_id)
    if max_mode:
        body += upstream.pb_bool(4, True)
    if aspect_ratio:
        body += upstream.pb_str(5, aspect_ratio)
    return body


def encode_generate_image_json(
    description: str,
    *,
    reference_images: list[dict[str, str]] | None = None,
    model_id: str = "",
    max_mode: bool = False,
    aspect_ratio: str | None = None,
) -> bytes:
    """Connect-JSON unary body (camelCase protobuf JSON names)."""
    if not description or not str(description).strip():
        raise ValueError("description/prompt is required")
    payload: dict[str, Any] = {"description": str(description)}
    refs: list[dict[str, str]] = []
    for image in reference_images or []:
        data = str(image.get("data") or "")
        if not data:
            continue
        refs.append(
            {
                "data": data,
                "mimeType": str(image.get("mime_type") or image.get("mimeType") or "image/png"),
            }
        )
    if refs:
        payload["referenceImages"] = refs
    if model_id:
        payload["modelId"] = model_id
    if max_mode:
        payload["maxMode"] = True
    if aspect_ratio:
        payload["aspectRatio"] = aspect_ratio
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _first_str(fields: dict[int, list], field: int) -> str:
    values = fields.get(field) or []
    if not values:
        return ""
    raw = values[0]
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(raw)


def _first_bool(fields: dict[int, list], field: int) -> bool:
    values = fields.get(field) or []
    if not values:
        return False
    raw = values[0]
    if isinstance(raw, int):
        return bool(raw)
    return False


def _first_int(fields: dict[int, list], field: int) -> int | None:
    values = fields.get(field) or []
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, int):
        return raw
    return None


def decode_generate_image_response_proto(raw: bytes) -> dict[str, Any]:
    """Decode ``RunGenerateImageResponse`` from protobuf bytes."""
    fields, _ = upstream.pb_decode(raw)
    if 1 in fields:
        success_fields, _ = upstream.pb_decode(fields[1][0])
        return {
            "ok": True,
            "image_data": _first_str(success_fields, 1),
            "mime_type": _first_str(success_fields, 2) or "image/png",
        }
    if 2 in fields:
        err_fields, _ = upstream.pb_decode(fields[2][0])
        return {
            "ok": False,
            "error": _first_str(err_fields, 1) or "image generation failed",
            "model_restricted": _first_bool(err_fields, 2),
            "provider_status_code": _first_int(err_fields, 3),
            "content_safety_blocked": _first_bool(err_fields, 4),
        }
    return {"ok": False, "error": "empty RunGenerateImageResponse"}


def decode_generate_image_response_json(raw: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Decode Connect-JSON unary response (camelCase or snake_case)."""
    if isinstance(raw, dict):
        payload = raw
    else:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        payload = json.loads(text) if text.strip() else {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "non-object image generation response"}

    success = payload.get("success")
    if isinstance(success, dict):
        image_data = success.get("imageData") or success.get("image_data") or ""
        image_url = success.get("imageUrl") or success.get("image_url") or success.get("url") or ""
        image_bytes = success.get("imageBytes") or success.get("image_bytes")
        if not image_data and isinstance(image_bytes, list):
            try:
                image_data = base64.b64encode(bytes(image_bytes)).decode("ascii")
            except (TypeError, ValueError):
                image_data = ""
        mime = success.get("mimeType") or success.get("mime_type") or "image/png"
        return {
            "ok": True,
            "image_data": str(image_data),
            "image_url": str(image_url),
            "mime_type": str(mime),
        }

    error = payload.get("error")
    if isinstance(error, dict):
        return {
            "ok": False,
            "error": str(error.get("error") or "image generation failed"),
            "model_restricted": bool(error.get("modelRestricted") or error.get("model_restricted")),
            "provider_status_code": error.get("providerStatusCode")
            if error.get("providerStatusCode") is not None
            else error.get("provider_status_code"),
            "content_safety_blocked": bool(
                error.get("contentSafetyBlocked") or error.get("content_safety_blocked")
            ),
        }
    if isinstance(error, str) and error:
        return {"ok": False, "error": error}

    # Some gateways flatten success fields onto the root.
    image_data = payload.get("imageData") or payload.get("image_data")
    image_url = payload.get("imageUrl") or payload.get("image_url") or payload.get("url")
    if (isinstance(image_data, str) and image_data) or (isinstance(image_url, str) and image_url):
        mime = payload.get("mimeType") or payload.get("mime_type") or "image/png"
        return {
            "ok": True,
            "image_data": image_data if isinstance(image_data, str) else "",
            "image_url": image_url if isinstance(image_url, str) else "",
            "mime_type": str(mime),
        }

    return {"ok": False, "error": "unrecognized image generation response"}


def decode_generate_image_response(raw: bytes, content_type: str = "") -> dict[str, Any]:
    """Decode JSON or protobuf response based on Content-Type / sniffing."""
    ctype = (content_type or "").lower()
    if "json" in ctype or (raw[:1] in (b"{", b"[") and b"proto" not in ctype.encode()):
        try:
            return decode_generate_image_response_json(raw)
        except json.JSONDecodeError:
            pass
    if "proto" in ctype or raw[:1] not in (b"{", b"["):
        return decode_generate_image_response_proto(raw)
    try:
        return decode_generate_image_response_json(raw)
    except json.JSONDecodeError:
        return decode_generate_image_response_proto(raw)


def image_bytes_from_b64(image_data: str) -> bytes:
    """Decode base64 image payload (raw or data-URL)."""
    data = (image_data or "").strip()
    if data.startswith("data:") and "," in data:
        data = data.split(",", 1)[1]
    # tolerate whitespace / missing padding
    data = "".join(data.split())
    pad = (-len(data)) % 4
    if pad:
        data += "=" * pad
    return base64.b64decode(data)


def image_bytes_from_source(
    source: str | bytes,
    *,
    url_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
) -> tuple[bytes, str | None]:
    """Decode raw bytes, base64/data URLs, or fetch an http(s) image URL.

    The returned MIME type is populated for data URLs and URL responses when
    available. ``url_fetcher`` is injectable for offline tests.
    """
    if isinstance(source, bytes):
        return source, None
    value = (source or "").strip()
    if value.startswith("data:") and "," in value:
        header, _ = value.split(",", 1)
        mime = header[5:].split(";", 1)[0] or None
        return image_bytes_from_b64(value), mime
    if value.startswith(("https://", "http://")):
        if url_fetcher is None:
            def default_fetcher(url: str) -> tuple[bytes, str]:
                request = urllib.request.Request(url, headers={"accept": "image/*"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read(32 * 1024 * 1024 + 1)
                    if len(raw) > 32 * 1024 * 1024:
                        raise ImageGenError("generated image URL exceeded 32 MiB")
                    return raw, response.headers.get("content-type") or ""
            url_fetcher = default_fetcher
        raw, mime = url_fetcher(value)
        return raw, mime or None
    return image_bytes_from_b64(value), None


def extension_for_mime(mime_type: str) -> str:
    mime = (mime_type or "image/png").split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(mime) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    return ext


def default_http_transport(
    url: str, body: bytes, headers: dict[str, str], *, timeout_s: float = 120.0
) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return (
                int(response.status),
                response.read(),
                response.headers.get("content-type") or "",
            )
    except urllib.error.HTTPError as error:
        return (
            int(error.code),
            error.read() if error.fp is not None else b"",
            error.headers.get("content-type") if error.headers else "",
        )


def session_headers(
    session_token: str,
    meta: dict[str, str],
    machine_id: str,
    *,
    content_type: str = "application/json",
    timeout_ms: int = 120000,
) -> dict[str, str]:
    return {
        "authorization": f"Bearer {session_token}",
        "content-type": content_type,
        "connect-protocol-version": "1",
        "connect-timeout-ms": str(timeout_ms),
        "x-cursor-checksum": upstream.cursor_checksum(machine_id),
        **meta,
    }


def run_generate_image(
    *,
    session_token: str,
    description: str,
    backend_url: str = upstream.DEFAULT_BACKEND_URL,
    meta: dict[str, str] | None = None,
    machine_id: str | None = None,
    model_id: str = "",
    max_mode: bool = False,
    aspect_ratio: str | None = None,
    reference_images: list[dict[str, str]] | None = None,
    encoding: str = "json",
    timeout_ms: int = 120000,
    transport: TransportFn | None = None,
) -> dict[str, Any]:
    """Call ``AiService/RunGenerateImage`` and return decoded success fields.

    ``transport(url, body, headers) -> (status, raw, content_type)`` is injectable
    for offline tests. ``encoding`` is ``json`` (default unary) or ``proto``.
    """
    if not session_token:
        raise ImageGenError("session token is required for RunGenerateImage")
    meta = dict(meta or {})
    machine_id = machine_id or upstream.load_machine_id()
    encoding = (encoding or "json").lower().strip()
    if encoding not in {"json", "proto"}:
        raise ValueError("encoding must be 'json' or 'proto'")

    if encoding == "json":
        body = encode_generate_image_json(
            description,
            reference_images=reference_images,
            model_id=model_id,
            max_mode=max_mode,
            aspect_ratio=aspect_ratio,
        )
        content_type = "application/json"
    else:
        body = encode_generate_image_request(
            description,
            reference_images=reference_images,
            model_id=model_id,
            max_mode=max_mode,
            aspect_ratio=aspect_ratio,
        )
        content_type = "application/proto"

    headers = session_headers(
        session_token,
        meta,
        machine_id,
        content_type=content_type,
        timeout_ms=timeout_ms,
    )
    url = backend_url.rstrip("/") + GENERATE_IMAGE_PATH
    send = transport or (
        lambda u, b, h: default_http_transport(u, b, h, timeout_s=timeout_ms / 1000)
    )
    status, raw, resp_ctype = send(url, body, headers)
    if status != 200:
        snippet = raw.decode("utf-8", "replace")[:300] if raw else ""
        raise ImageGenError(
            f"RunGenerateImage HTTP {status}: {snippet or 'empty body'}",
            http_status=status,
        )
    decoded = decode_generate_image_response(raw, resp_ctype)
    # The published runtime schema names this field image_data (base64), but
    # tolerate deployments returning an URL in that string field.
    image_data = decoded.get("image_data")
    if isinstance(image_data, str) and image_data.startswith(("https://", "http://")):
        decoded["image_url"] = image_data
        decoded["image_data"] = ""
    if not decoded.get("ok"):
        raise ImageGenError(
            str(decoded.get("error") or "image generation failed"),
            http_status=status,
            model_restricted=bool(decoded.get("model_restricted")),
            content_safety_blocked=bool(decoded.get("content_safety_blocked")),
            provider_status_code=(
                int(decoded["provider_status_code"])
                if isinstance(decoded.get("provider_status_code"), int)
                else None
            ),
        )
    if not decoded.get("image_data") and not decoded.get("image_url"):
        raise ImageGenError("RunGenerateImage success missing image_data/image_url")
    return decoded


class MediaStore:
    """Filesystem media store with a small JSON index under ``media/``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_MEDIA_DIR
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        # Keep directory but ignore generated blobs via .gitignore.
        gitkeep = self.root / ".gitkeep"
        if not gitkeep.exists():
            try:
                gitkeep.write_text("", encoding="utf-8")
            except OSError:
                pass

    def _index_path(self) -> Path:
        return self.root / INDEX_NAME

    def _load_index(self) -> dict[str, Any]:
        path = self._index_path()
        if not path.is_file():
            return {"items": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"items": {}}
        if not isinstance(data, dict):
            return {"items": {}}
        items = data.get("items")
        if not isinstance(items, dict):
            data["items"] = {}
        return data

    def _save_index(self, data: dict[str, Any]) -> None:
        path = self._index_path()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def save(
        self,
        image_data_b64: str | bytes,
        mime_type: str = "image/png",
        *,
        prompt: str = "",
        model: str = "",
        aspect_ratio: str | None = None,
        media_id: str | None = None,
        url_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
    ) -> dict[str, Any]:
        raw, detected_mime = image_bytes_from_source(image_data_b64, url_fetcher=url_fetcher)
        if detected_mime:
            mime_type = detected_mime
        mid = media_id or uuid.uuid4().hex
        ext = extension_for_mime(mime_type)
        filename = f"{mid}{ext}"
        path = self.root / filename
        with self.lock:
            path.write_bytes(raw)
            index = self._load_index()
            entry = {
                "id": mid,
                "filename": filename,
                "mime_type": mime_type or "image/png",
                "created": int(time.time()),
                "prompt": prompt,
                "model": model,
                "aspect_ratio": aspect_ratio,
                "bytes": len(raw),
            }
            index.setdefault("items", {})[mid] = entry
            self._save_index(index)
        return dict(entry)

    def get(self, media_id: str) -> dict[str, Any] | None:
        with self.lock:
            index = self._load_index()
            item = (index.get("items") or {}).get(media_id)
            return dict(item) if isinstance(item, dict) else None

    def resolve_path(self, media_id: str) -> Path | None:
        item = self.get(media_id)
        if not item:
            return None
        filename = str(item.get("filename") or "")
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return None
        path = (self.root / filename).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None



    def list_items(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 200), 1000))
        with self.lock:
            index = self._load_index()
            items = []
            for item in (index.get("items") or {}).values():
                if isinstance(item, dict) and item.get("id"):
                    items.append(dict(item))
            items.sort(key=lambda entry: int(entry.get("created") or 0), reverse=True)
            return items[:limit]

    def delete(self, media_id: str) -> bool:
        media_id = str(media_id or "").strip()
        if not media_id or "/" in media_id or ".." in media_id:
            raise ValueError("invalid media id")
        with self.lock:
            index = self._load_index()
            items = index.setdefault("items", {})
            item = items.pop(media_id, None)
            if not isinstance(item, dict):
                raise KeyError(media_id)
            filename = str(item.get("filename") or "")
            if filename and "/" not in filename and '\\' not in filename and ".." not in filename:
                path = self.root / filename
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            self._save_index(index)
            return True


def openai_images_response(
    *,
    b64_json: str,
    created: int | None = None,
    url: str | None = None,
    response_format: str = "url",
    revised_prompt: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible ``/v1/images/generations`` payload."""
    item: dict[str, Any] = {}
    fmt = (response_format or "url").lower()
    if fmt == "b64_json":
        item["b64_json"] = b64_json
    else:
        if url:
            item["url"] = url
        else:
            item["b64_json"] = b64_json
    if revised_prompt:
        item["revised_prompt"] = revised_prompt
    return {"created": int(created or time.time()), "data": [item]}
