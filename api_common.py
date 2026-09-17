"""Shared helpers for the local OpenAI-compatible HTTP adapters."""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urlparse


TOOL_CALL_ID_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
)

IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})
TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;charset=[^;,]+)?;base64,(.+)$", re.IGNORECASE | re.DOTALL)


class ClientDisconnected(Exception):
    """The HTTP client closed the socket while a response was being written."""


def normalize_tool_call_id(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = "".join(
        character if character in TOOL_CALL_ID_SAFE_CHARS else "_" for character in raw
    )
    return normalized[:240]


def content_text(content: Any) -> str:
    """Flatten content to text. Prefer ``normalize_message_content`` when images matter."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            if part.get("type") in TEXT_PART_TYPES:
                parts.append(str(part.get("text", "")))
            elif part.get("type") in IMAGE_PART_TYPES:
                # Kept for callers that only need a string summary; native encoding
                # uses normalize_message_content instead of this placeholder.
                parts.append("[image]")
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(parts)


def _image_url_from_part(part: dict[str, Any]) -> tuple[str, str]:
    """Return (url_or_data_url, media_type_hint) from an OpenAI-style image part."""
    media_type = ""
    if isinstance(part.get("media_type"), str):
        media_type = part["media_type"]
    elif isinstance(part.get("mime_type"), str):
        media_type = part["mime_type"]

    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = str(image_url.get("url") or "")
        if not media_type and isinstance(image_url.get("media_type"), str):
            media_type = image_url["media_type"]
        return url, media_type
    if isinstance(image_url, str):
        return image_url, media_type

    if isinstance(part.get("image"), str):
        return part["image"], media_type
    if isinstance(part.get("url"), str):
        return part["url"], media_type

    # Responses API input_image often uses ``image_url`` string or ``file_id``.
    if isinstance(part.get("file_id"), str) and part["file_id"]:
        return f"file_id:{part['file_id']}", media_type
    return "", media_type


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Return (media_type, base64_payload) for a data: URL, else None."""
    match = DATA_URL_RE.match(url.strip())
    if not match:
        return None
    media_type = (match.group(1) or "application/octet-stream").strip()
    payload = match.group(2).strip()
    try:
        # Validate base64; keep the original string for the wire.
        base64.b64decode(payload, validate=False)
    except Exception:
        return None
    return media_type, payload


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_message_content(content: Any) -> dict[str, Any]:
    """Split OpenAI / Responses content into text chunks and image descriptors.

    Returns::

        {
          "text": "joined text",
          "images": [
             {"kind": "url", "url": "https://...", "media_type": ""},
             {"kind": "base64", "data": "...", "media_type": "image/png"},
             ...
          ],
          "has_images": bool,
        }
    """
    images: list[dict[str, Any]] = []
    texts: list[str] = []

    def add_image(url: str, media_type: str = "") -> None:
        url = (url or "").strip()
        if not url:
            return
        if url.startswith("file_id:"):
            # Upstream sand inference has no file_id upload path here; skip.
            return
        parsed = parse_data_url(url)
        if parsed:
            mt, payload = parsed
            images.append(
                {
                    "kind": "base64",
                    "data": payload,
                    "media_type": media_type or mt or "image/png",
                }
            )
            return
        if is_http_url(url) or url.startswith("data:"):
            images.append({"kind": "url", "url": url, "media_type": media_type})
            return
        # Bare base64 without data: prefix — treat as PNG by default when long.
        if len(url) > 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", url or ""):
            images.append(
                {
                    "kind": "base64",
                    "data": re.sub(r"\s+", "", url),
                    "media_type": media_type or "image/png",
                }
            )

    if content is None:
        return {"text": "", "images": [], "has_images": False}
    if isinstance(content, str):
        return {"text": content, "images": [], "has_images": False}
    if not isinstance(content, list):
        return {
            "text": json.dumps(content, ensure_ascii=False),
            "images": [],
            "has_images": False,
        }

    for part in content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type in TEXT_PART_TYPES:
            texts.append(str(part.get("text", "")))
        elif part_type in IMAGE_PART_TYPES:
            url, media_type = _image_url_from_part(part)
            add_image(url, media_type)
        elif part_type in {"file", "input_file"} and str(part.get("media_type") or "").startswith(
            "image/"
        ):
            # File parts that are images (Responses / AI SDK style).
            data = part.get("file_data") or part.get("data") or ""
            if isinstance(data, str) and data.startswith("data:"):
                add_image(data, str(part.get("media_type") or ""))
            elif isinstance(data, str) and data:
                add_image(
                    f"data:{part.get('media_type') or 'image/png'};base64,{data}",
                    str(part.get("media_type") or "image/png"),
                )
            elif isinstance(part.get("url"), str):
                add_image(part["url"], str(part.get("media_type") or ""))
        else:
            # Unknown structured parts: keep a JSON text fallback so nothing is lost.
            if part_type not in {"refusal"}:
                texts.append(json.dumps(part, ensure_ascii=False))

    text = "\n".join(item for item in texts if item)
    return {"text": text, "images": images, "has_images": bool(images)}


def preserve_structured_content(content: Any) -> Any:
    """Keep list content when it contains images; otherwise flatten to text."""
    normalized = normalize_message_content(content)
    if normalized["has_images"] and isinstance(content, list):
        return content
    if isinstance(content, str) or content is None:
        return content if content is not None else ""
    return normalized["text"]
