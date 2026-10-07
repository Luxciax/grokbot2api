#!/usr/bin/env python3
"""grokbot2api: bridge Grok Build to Cursor's native inference protobuf.

The loopback HTTP edge is OpenAI-compatible because that is Grok Build's
custom-model interface. Messages, tool schemas, tool calls, and tool results
are carried over the upstream ``aiserver.v1.InferenceService.Stream`` protocol.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import http.client
import importlib.util
import json
import os
import queue
import re
import ssl
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from api_common import (
    ClientDisconnected,
    content_text,
    normalize_message_content,
    normalize_tool_call_id,
)
import image_gen
import grokbot_chat
from messages_api import MessagesApiMixin
from model_catalogue import DEFAULT_ALIAS, ModelCatalogue, ModelSpec
from responses_api import ResponsesApiMixin


DEFAULT_UPSTREAM_SCRIPT = Path(__file__).with_name("sand_inference.py")
DEFAULT_CACHE = Path("/tmp/grokbot2api-token.json")
MAX_REQUEST_BYTES = 16 * 1024 * 1024
STREAM_HEARTBEAT_SECONDS = 1.0

# Observed (2026-09-15): requested output ceilings below ~160 are rejected upstream with
# "Provider exceeded max output tokens." — 8/32/64/100/128 all fail, 160 and above are
# accepted. Clients that ask for small budgets (titles, one-line answers) therefore get a
# confusing 502. max_tokens is a ceiling, not a target, so raise small ones instead.
DEFAULT_MIN_MAX_TOKENS = 512
__version__ = "0.3.9"
AUDIT_RING_SIZE = 200
TRANSIENT_UPSTREAM_STATUSES = frozenset({429, 502, 503})
RETRY_BACKOFF_SECONDS = 0.6

def coerce_error_message(value: Any) -> str:
    """Flatten nested upstream / OpenAI-style error payloads into a single string.

    Connect trailers often arrive as a dict (or ``str(dict)`` from an older path)
    with ``details[].debug.error == ERROR_NOT_HIGH_ENOUGH_PERMISSIONS``. Prefer a
    short Chinese explanation for that code over the raw nested payload.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        parsed = _try_parse_embedded_error(text)
        if parsed is not None:
            return coerce_error_message(parsed)
        mapped = _friendly_upstream_code_message(text)
        return mapped or text
    if isinstance(value, dict):
        mapped = _friendly_upstream_code_message(_extract_upstream_error_code(value) or "")
        if mapped:
            return mapped
        for key in ("message", "detail", "title", "error", "msg"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                # Prefer a code-based friendly string when the human message is generic.
                nested_code = _extract_upstream_error_code(value)
                mapped = _friendly_upstream_code_message(nested_code or "")
                if mapped and inner.strip().lower() in {"access denied.", "access denied", "permission denied"}:
                    return mapped
                return inner.strip()
            if isinstance(inner, dict):
                nested = coerce_error_message(inner)
                if nested:
                    return nested
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple)):
        parts = [coerce_error_message(item) for item in value]
        return "; ".join(part for part in parts if part) or str(value)
    return str(value)


def _try_parse_embedded_error(text: str) -> Any | None:
    """Parse a JSON object or Python ``str(dict)`` trailer payload."""
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        import ast

        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    return parsed if isinstance(parsed, (dict, list, tuple)) else None


def _extract_upstream_error_code(value: Any) -> str | None:
    if isinstance(value, str):
        match = re.search(r"ERROR_[A-Z0-9_]+", value)
        return match.group(0) if match else None
    if not isinstance(value, dict):
        return None
    for key in ("error", "code", "error_code"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.startswith("ERROR_"):
            return raw.strip()
        if isinstance(raw, dict):
            nested = _extract_upstream_error_code(raw)
            if nested:
                return nested
    details = value.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            debug = item.get("debug")
            if isinstance(debug, dict):
                nested = _extract_upstream_error_code(debug)
                if nested:
                    return nested
            nested = _extract_upstream_error_code(item)
            if nested:
                return nested
    return None



def _agent_strip_tools_enabled() -> bool:
    """When chat_mode=agent and the client sent tools, strip them and stay on
    GrokBotService (default True). Set GROKBOT_AGENT_STRIP_TOOLS=0/false/no/off
    to call InferenceService/Stream instead (accounts that have Stream access).
    """
    raw = (os.environ.get("GROKBOT_AGENT_STRIP_TOOLS") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}

def _friendly_upstream_code_message(code: str) -> str:
    """Map known Cursor/ai-server error codes to short Chinese messages."""
    if not code:
        return ""
    code = code.strip()
    mapping = {
        "ERROR_NOT_HIGH_ENOUGH_PERMISSIONS": (
            "上游拒绝 InferenceService/Stream（ERROR_NOT_HIGH_ENOUGH_PERMISSIONS）。"
            "常见原因：客户端附带了 tools，网关走了 Stream 而非 Agent。"
            "默认 chat_mode=agent 会剥离 tools；或设 GROKBOT_CHAT_MODE=agent，"
            "勿设 stream。这不是 sbi_/JWT 过期。"
        ),
        "ERROR_NOT_LOGGED_IN": (
            "上游未登录（ERROR_NOT_LOGGED_IN）：推理令牌无效或已过期，请重新导入/粘贴 sbi_ 续期凭证。"
        ),
        "ERROR_RATE_LIMITED": "请求过于频繁（ERROR_RATE_LIMITED），请稍后重试。",
        "ERROR_USAGE_EXCEEDED": "用量已耗尽（ERROR_USAGE_EXCEEDED），请等待周期重置或升级套餐。",
    }
    if code in mapping:
        return mapping[code]
    if code.startswith("ERROR_"):
        return f"上游拒绝（{code}）。"
    return ""


def api_error_payload(message: Any, err_type: str = "invalid_request_error", **extra: Any) -> dict[str, Any]:
    """OpenAI-shaped error object plus a flat string message for stubborn clients."""
    text_msg = coerce_error_message(message) or "unknown error"
    error: dict[str, Any] = {"message": text_msg, "type": err_type}
    error.update(extra)
    return {"error": error, "message": text_msg}


def load_upstream(path: Path) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(f"upstream script not found: {path}")
    spec = importlib.util.spec_from_file_location("sand_inference_upstream", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import upstream script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module




def encode_proto_value(upstream: ModuleType, value: Any) -> bytes:
    """Encode google.protobuf.Value without requiring protobuf as a dependency."""
    if value is None:
        return upstream.pb_var(1, 0)
    if isinstance(value, bool):
        return upstream.pb_bool(4, value)
    if isinstance(value, (int, float)):
        return upstream._key(2, 1) + struct.pack("<d", float(value))
    if isinstance(value, str):
        return upstream.pb_str(3, value)
    if isinstance(value, dict):
        return upstream.pb_msg(5, encode_proto_struct(upstream, value))
    if isinstance(value, list):
        list_value = b"".join(upstream.pb_msg(1, encode_proto_value(upstream, item)) for item in value)
        return upstream.pb_msg(6, list_value)
    return upstream.pb_str(3, str(value))


def encode_proto_struct(upstream: ModuleType, value: dict[str, Any]) -> bytes:
    body = b""
    for key, item in value.items():
        entry = upstream.pb_str(1, str(key)) + upstream.pb_msg(2, encode_proto_value(upstream, item))
        body += upstream.pb_msg(1, entry)
    return body


def schema_argument_hint(schema: dict[str, Any]) -> str:
    """Compact a JSON Schema into text for providers rejecting tool.parameters."""
    properties = schema.get("properties")
    required = set(schema.get("required") or [])
    if not isinstance(properties, dict) or not properties:
        return "Arguments: no named arguments."
    items: list[str] = []
    for name, definition in properties.items():
        if not isinstance(definition, dict):
            definition = {}
        arg_type = definition.get("type", "any")
        if isinstance(arg_type, list):
            arg_type = "|".join(str(item) for item in arg_type)
        enum = definition.get("enum")
        enum_hint = ""
        if isinstance(enum, list) and enum:
            enum_hint = " enum=" + "|".join(str(item) for item in enum[:12])
        description = str(definition.get("description") or "").replace("\n", " ").strip()
        if len(description) > 120:
            description = description[:117] + "..."
        required_hint = " required" if name in required else " optional"
        description_hint = f" - {description}" if description else ""
        items.append(f"{name}:{arg_type}{required_hint}{enum_hint}{description_hint}")
    hint = "Arguments JSON object: " + "; ".join(items)
    return hint[:1800]



def encode_inference_text_part(upstream: ModuleType, text: str) -> bytes:
    """InferenceContentPart.text = InferenceTextPart { string text = 1 }."""
    return upstream.pb_msg(1, upstream.pb_str(1, text))


def encode_inference_image_part(upstream: ModuleType, image: dict[str, Any]) -> bytes:
    """Best-effort InferenceImagePart aligned with Cursor SdkImage / AI SDK ImagePart.

    message InferenceImagePart {
      oneof source {
        InferenceImageUrl url = 1;   // { string url = 1 }
        InferenceImageData data = 2; // { string data = 1; string media_type = 2 }
      }
      optional string media_type = 3;
    }
    """
    media_type = str(image.get("media_type") or "")
    body = b""
    if image.get("kind") == "base64":
        data = upstream.pb_str(1, str(image.get("data") or ""))
        if media_type:
            data += upstream.pb_str(2, media_type)
        body += upstream.pb_msg(2, data)
    else:
        url_msg = upstream.pb_str(1, str(image.get("url") or ""))
        body += upstream.pb_msg(1, url_msg)
    if media_type:
        body += upstream.pb_str(3, media_type)
    # Wrap as InferenceContentPart.image = 2
    return upstream.pb_msg(2, body)


def encode_content_parts(upstream: ModuleType, text: str, images: list[dict[str, Any]]) -> bytes:
    """InferenceContentParts { repeated InferenceContentPart parts = 1 }."""
    parts = b""
    if text:
        parts += upstream.pb_msg(1, encode_inference_text_part(upstream, text))
    for image in images:
        parts += upstream.pb_msg(1, encode_inference_image_part(upstream, image))
    return parts


def encode_message_content_fields(upstream: ModuleType, content: Any) -> bytes:
    """Write InferenceCoreMessage content oneof: text=2 or parts=3."""
    normalized = normalize_message_content(content)
    if normalized["has_images"]:
        return upstream.pb_msg(
            3, encode_content_parts(upstream, normalized["text"], normalized["images"])
        )
    text = normalized["text"]
    if text:
        return upstream.pb_str(2, text)
    return b""


def tool_name_index(messages: list[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            call_id = normalize_tool_call_id(call.get("id"))
            name = function.get("name")
            if call_id and isinstance(name, str):
                names[call_id] = name
    return names


def encode_native_message(upstream: ModuleType, message: dict[str, Any], known_tools: dict[str, str]) -> bytes:
    roles = {"user": 1, "assistant": 2, "tool": 3, "system": 4, "developer": 4}
    role = str(message.get("role", "user"))
    body = upstream.pb_var(1, roles.get(role, 0))

    if role == "assistant":
        body += encode_message_content_fields(upstream, message.get("content"))
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            call_id = normalize_tool_call_id(call.get("id"))
            name = str(function.get("name") or "")
            raw_args = function.get("arguments", "{}")
            if not isinstance(raw_args, str):
                raw_args = json.dumps(raw_args, ensure_ascii=False, separators=(",", ":"))
            tool_call = upstream.pb_str(1, call_id) + upstream.pb_str(2, name)
            try:
                parsed_args = json.loads(raw_args)
                if isinstance(parsed_args, dict):
                    tool_call += upstream.pb_msg(3, encode_proto_struct(upstream, parsed_args))
            except json.JSONDecodeError:
                pass
            tool_call += upstream.pb_str(4, raw_args)
            body += upstream.pb_msg(4, tool_call)
    elif role == "tool":
        call_id = normalize_tool_call_id(message.get("tool_call_id"))
        name = str(message.get("name") or known_tools.get(call_id, ""))
        result = content_text(message.get("content"))
        result_part = upstream.pb_str(1, call_id) + upstream.pb_str(2, name)
        result_part += upstream.pb_msg(3, encode_proto_value(upstream, result))
        if message.get("is_error"):
            result_part += upstream.pb_bool(4, True)
        tool_content = upstream.pb_msg(1, result_part)
        body += upstream.pb_msg(6, tool_content)
    else:
        body += encode_message_content_fields(upstream, message.get("content"))
    return body


def encode_native_request(
    upstream: ModuleType,
    messages: list[Any],
    tools: list[Any],
    model: str,
    invocation_id: str,
    conversation_id: str,
    max_mode: bool,
    request: dict[str, Any],
    model_params: list[tuple[str, str]] | None = None,
) -> bytes:
    body = b""
    known_tools = tool_name_index(messages)
    for message in messages:
        if isinstance(message, dict):
            body += upstream.pb_msg(1, encode_native_message(upstream, message, known_tools))

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        parameters = function.get("parameters")
        description = str(function.get("description") or "")
        if isinstance(parameters, dict):
            description = f"{description}\n\n{schema_argument_hint(parameters)}".strip()
        # grok-4.6 currently rejects any present InferenceAgentTool.parameters
        # Struct with provider status 422. Keep the native tool call protocol,
        # but carry a compact parameter signature in the description.
        proto_tool = upstream.pb_str(1, name)
        proto_tool += upstream.pb_str(2, description[:4000])
        body += upstream.pb_msg(2, proto_tool)

    params = list(model_params) if model_params is not None else list(upstream.DEFAULT_MODEL_PARAMS)
    # Prefer sand_inference.encode_requested_model when available (variant/built_in fields).
    encode_rm = getattr(upstream, "encode_requested_model", None)
    if callable(encode_rm):
        requested = encode_rm(
            model,
            max_mode,
            params,
            is_variant_string_representation=not bool(params),
        )
    else:
        requested = upstream.pb_str(1, model)
        if max_mode:
            requested += upstream.pb_bool(2, True)
        for parameter_id, parameter_value in params:
            parameter = upstream.pb_str(1, parameter_id) + upstream.pb_str(2, parameter_value)
            requested += upstream.pb_msg(3, parameter)
        if not params:
            requested += upstream.pb_bool(8, True)  # is_variant_string_representation
    body += upstream.pb_msg(7, requested)
    body += upstream.pb_str(6, invocation_id)
    if conversation_id:
        body += upstream.pb_str(8, conversation_id)
        body += upstream.pb_str(12, conversation_id)

    model_config = b""
    if isinstance(request.get("max_tokens"), int):
        model_config += upstream.pb_var(1, request["max_tokens"])
    if isinstance(request.get("temperature"), (int, float)):
        model_config += upstream._key(2, 5) + struct.pack("<f", float(request["temperature"]))
    if isinstance(request.get("top_p"), (int, float)):
        model_config += upstream._key(3, 5) + struct.pack("<f", float(request["top_p"]))
    for stop in request.get("stop") if isinstance(request.get("stop"), list) else []:
        if isinstance(stop, str):
            model_config += upstream.pb_str(4, stop)
    if model_config:
        body += upstream.pb_msg(4, model_config)
    return body


def clamp_output_ceiling(request: dict[str, Any], min_max_tokens: int) -> dict[str, Any]:
    """Raise a requested output ceiling to the smallest value the upstream lane accepts.

    Returns the input unchanged when there is nothing to raise. The input mapping is never
    mutated: a clamped copy is returned instead.
    """
    requested = request.get("max_tokens")
    if (
        not isinstance(min_max_tokens, int)
        or min_max_tokens <= 0
        or not isinstance(requested, int)
        or requested >= min_max_tokens
    ):
        return request
    clamped = dict(request)
    clamped["max_tokens"] = min_max_tokens
    return clamped


def first_text(fields: dict[int, list[Any]], field: int) -> str:
    values = fields.get(field, [])
    if not values:
        return ""
    value = values[0]
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def first_int(fields: dict[int, list[Any]], field: int, default: int = 0) -> int:
    values = fields.get(field, [])
    return int(values[0]) if values else default


def decode_extended_usage(upstream: ModuleType, raw: bytes) -> dict[str, int]:
    fields, _ = upstream.pb_decode(raw)
    return {
        "prompt_tokens": first_int(fields, 1),
        "completion_tokens": first_int(fields, 2),
        "cached_prompt_tokens": first_int(fields, 3),
        "context_window": first_int(fields, 5),
    }



def _harvest_image_description_strings(upstream: ModuleType, raw: bytes) -> str:
    """Best-effort: collect UTF-8 strings from InferenceImageDescriptionsInfo."""
    chunks: list[str] = []

    def walk(buf: bytes, depth: int = 0) -> None:
        if depth > 6 or not buf:
            return
        try:
            fields, _ = upstream.pb_decode(buf)
        except Exception:
            return
        for values in fields.values():
            for value in values:
                if isinstance(value, bytes):
                    # Prefer nested messages; also accept plain UTF-8 strings.
                    if len(value) >= 2:
                        walk(value, depth + 1)
                    try:
                        text = value.decode("utf-8")
                    except Exception:
                        continue
                    if text and text.isprintable() and len(text) < 8000:
                        chunks.append(text)

    walk(raw)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for item in chunks:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return "\n".join(ordered)


def decode_native_response(upstream: ModuleType, raw: bytes, status: int, request_id: str, model: str) -> dict[str, Any]:
    if status != 200:
        return {
            "ok": False,
            "httpStatus": status,
            "error": raw[:800].decode("utf-8", "replace"),
            "requestId": request_id,
            "modelId": model,
        }

    texts: list[str] = []
    thinking: list[str] = []
    errors: list[Any] = []
    image_descriptions: list[str] = []
    response_model = model
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    extended_usage: dict[str, int] = {}
    pending: dict[str, dict[str, Any]] = {}
    completed_calls: list[dict[str, Any]] = []
    envelopes = upstream.iter_envelopes_from_bytes(raw)

    for flags, payload in envelopes:
        if flags & 2:
            try:
                trailer = json.loads(payload.decode("utf-8") or "{}")
                if isinstance(trailer, dict) and trailer.get("error"):
                    errors.append(trailer["error"])
            except Exception:
                pass
            continue
        if flags & 1:
            payload = gzip.decompress(payload)
        outer, _ = upstream.pb_decode(payload)

        for part_raw in outer.get(1, []):
            part, _ = upstream.pb_decode(part_raw)
            text = first_text(part, 1)
            if text:
                texts.append(text)

        for part_raw in outer.get(9, []):
            part, _ = upstream.pb_decode(part_raw)
            text = first_text(part, 1)
            if text:
                thinking.append(text)

        for part_raw in outer.get(2, []):
            part, _ = upstream.pb_decode(part_raw)
            call_id = normalize_tool_call_id(first_text(part, 1))
            name = first_text(part, 2)
            args_delta = first_text(part, 3)
            is_complete = bool(first_int(part, 4))
            index = first_int(part, 5, -1)
            key = call_id or (f"index:{index}" if index >= 0 else f"pending:{len(pending)}")
            state = pending.setdefault(key, {"id": call_id, "name": name, "args": "", "index": index})
            if call_id:
                state["id"] = call_id
            if name:
                state["name"] = name
            if args_delta:
                if is_complete:
                    state["args"] = args_delta
                else:
                    state["args"] += args_delta
            if is_complete:
                completed_calls.append(
                    {
                        "id": state["id"] or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {"name": state["name"], "arguments": state["args"] or "{}"},
                    }
                )
                pending.pop(key, None)

        for usage_raw in outer.get(3, []):
            info, _ = upstream.pb_decode(usage_raw)
            prompt_tokens = first_int(info, 1)
            completion_tokens = first_int(info, 2)
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": first_int(info, 3, prompt_tokens + completion_tokens),
            }

        for usage_raw in outer.get(5, []):
            extended_usage = decode_extended_usage(upstream, usage_raw)

        for info_raw in outer.get(4, []):
            info, _ = upstream.pb_decode(info_raw)
            response_model = first_text(info, 2) or response_model
            error = first_text(info, 5)
            if error:
                errors.append(error)

        for error_raw in outer.get(8, []):
            error, _ = upstream.pb_decode(error_raw)
            errors.append(first_text(error, 1) or repr(error))

        # InferenceImageDescriptionsInfo (field 10) — best-effort string harvest.
        for desc_raw in outer.get(10, []):
            if isinstance(desc_raw, bytes):
                image_descriptions.append(_harvest_image_description_strings(upstream, desc_raw))

    image_descriptions = [item for item in image_descriptions if item]

    if extended_usage:
        prompt_tokens = usage["prompt_tokens"] or extended_usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"] or extended_usage["completion_tokens"]
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": usage["total_tokens"] or prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": extended_usage["cached_prompt_tokens"]},
        }

    return {
        "ok": not errors,
        "httpStatus": status,
        "requestId": request_id,
        "model": response_model,
        "modelId": model,
        "text": "".join(texts),
        "thinking": "".join(thinking),
        "tool_calls": completed_calls,
        "error": errors[0] if errors else None,
        "envelopes": len(envelopes),
        "usage": usage,
        "extended_usage": extended_usage,
        "image_descriptions": image_descriptions,
    }


def native_stream_llm(
    upstream: ModuleType,
    args: Any,
    access_token: str,
    messages: list[Any],
    tools: list[Any],
    request: dict[str, Any],
    model_params: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    invocation_id = str(uuid.uuid4())
    proto = encode_native_request(
        upstream,
        messages,
        tools,
        args.model,
        invocation_id,
        args.conversation_id,
        args.max_mode,
        request,
        model_params=model_params,
    )
    body = upstream.connect_envelope(proto, 0)
    parsed = urllib.parse.urlparse(args.backend_url)
    host = parsed.hostname or "api2.cursor.sh"
    port = parsed.port or (443 if parsed.scheme != "http" else 80)
    if parsed.scheme == "http":
        connection = http.client.HTTPConnection(host, port, timeout=args.timeout_ms / 1000)
    else:
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=args.timeout_ms / 1000,
            context=ssl.create_default_context(),
        )
    headers = upstream.inference_headers(args, access_token, upstream.load_machine_id(), request_id)
    connection.request("POST", upstream.INFERENCE_PATH, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    return decode_native_response(upstream, raw, status, request_id, args.model)


class SandBackend:
    def __init__(self, options: argparse.Namespace, catalogue: ModelCatalogue | None = None):
        self.options = options
        self.module = load_upstream(options.upstream_script)
        self.lock = threading.Lock()
        self.catalogue = catalogue or ModelCatalogue(
            default_alias=getattr(options, "default_alias", DEFAULT_ALIAS),
            config_path=getattr(options, "admin_config", None),
            fallback_upstream=options.model,
        )
        self.args = SimpleNamespace(
            backend_url=options.backend_url or self.module.DEFAULT_BACKEND_URL,
            credential=None,
            cache=options.cache,
            force_renew=False,
            renew_only=False,
            model=options.model,
            max_mode=options.max_mode,
            conversation_id=options.conversation_id,
            client_type=options.client_type,
            client_version=options.client_version,
            client_source=getattr(options, "client_source", None) or "sand-desktop",
            client_os=getattr(options, "client_os", None) or "",
            namespace=options.namespace,
            team_id=options.team_id,
            timeout_ms=options.timeout_ms,
            show_token=False,
        )
        self.args.conversation_id = self.module.resolve_conversation_id(self.args)

        # Allow HTTP listen (/health, /admin) without renewal; fail at inference time.
        env_credential = os.environ.get(self.module.CREDENTIAL_ENV, "").strip()
        self.args.credential = env_credential or None
        self.renewal_configured = bool(env_credential)
        if not self.renewal_configured:
            print(
                f"warning: {self.module.CREDENTIAL_ENV} not set; "
                "/health and /admin will work but inference will fail until it is configured",
                file=sys.stderr,
                flush=True,
            )
        media_dir = getattr(options, "media_dir", None)
        self.media_store = image_gen.MediaStore(Path(media_dir) if media_dir else None)

    def ensure_renewal_credential(self) -> None:
        """Raise when inference/usage needs sbi_ but none was configured at start."""
        if getattr(self.args, "credential", None):
            return
        env_name = getattr(self.module, "CREDENTIAL_ENV", "SAND_INFERENCE_RENEWAL_CREDENTIAL")
        env_credential = os.environ.get(env_name, "").strip()
        if env_credential:
            self.args.credential = env_credential
            self.renewal_configured = True
            return
        raise RuntimeError(
            f"set {env_name} to a valid credential before calling inference"
        )

    def sand_usage(self) -> dict[str, Any]:
        """Read the account's sand allowance status (percent used, reset time)."""
        self.ensure_renewal_credential()
        credential = self.module.load_renewal_credential(self.args)
        meta = self.module.client_meta(self.args)
        return self.module.fetch_sand_usage(
            credential, self.args.backend_url, meta, self.module.load_machine_id()
        )

    def session_token(self) -> str:
        """Return a Cursor *session* JWT for Dashboard / AiService / GrokBotService.

        Preference:
          1. ``SAND_SESSION_TOKEN`` / ``CURSOR_SESSION_TOKEN`` / ``GROKBOT_SESSION_ACCESS_TOKEN``
          2. Cached ``sessionToken`` from the renewal cache
          3. Mint via renewal credential exchange (``sessionToken`` / type=session)

        Not used by InferenceService/Stream (which needs grokBotToken).
        """
        for env_name in (
            "SAND_SESSION_TOKEN",
            "CURSOR_SESSION_TOKEN",
            "GROKBOT_SESSION_ACCESS_TOKEN",
        ):
            override = os.environ.get(env_name, "").strip()
            if override:
                return override
        self.ensure_renewal_credential()
        credential = self.module.load_renewal_credential(self.args)
        meta = self.module.client_meta(self.args)
        try:
            minted = self.module.get_access_token(self.args, credential, meta)
        except SystemExit as error:
            raise RuntimeError(str(error)) from error
        session = minted.get("sessionToken")
        if isinstance(session, str) and session:
            return session
        # Cache may predate sessionToken persistence — force a renew once.
        try:
            minted = self.module.get_access_token(self.args, credential, meta, force=True)
        except SystemExit as error:
            raise RuntimeError(str(error)) from error
        session = minted.get("sessionToken")
        if isinstance(session, str) and session:
            return session
        raise RuntimeError("renewal returned no session token for GrokBotService/Dashboard")

    def generate_image(
        self,
        prompt: str,
        *,
        model: str = "cursor-generate-image",
        aspect_ratio: str | None = None,
        size: str | None = None,
        response_format: str = "url",
        reference_images: list[dict[str, str]] | None = None,
        max_mode: bool = False,
        transport=None,
        media_store: "image_gen.MediaStore | None" = None,
        public_base: str = "",
    ) -> dict[str, Any]:
        """Call AiService/RunGenerateImage and persist under media/."""
        ratio = image_gen.normalize_aspect_ratio(aspect_ratio, size)
        meta = self.module.client_meta(self.args)
        machine_id = self.module.load_machine_id()
        with self.lock:
            session = self.session_token()
            result = image_gen.run_generate_image(
                session_token=session,
                description=prompt,
                backend_url=self.args.backend_url,
                meta=meta,
                machine_id=machine_id,
                model_id=model or "cursor-generate-image",
                max_mode=max_mode or bool(self.args.max_mode),
                aspect_ratio=ratio,
                reference_images=reference_images,
                timeout_ms=int(self.args.timeout_ms),
                transport=transport,
            )
        store = media_store or getattr(self, "media_store", None) or image_gen.MediaStore()
        self.media_store = store
        source = result.get("image_data") or result.get("image_url") or ""
        saved = store.save(
            source,
            result.get("mime_type") or "image/png",
            prompt=prompt,
            model=model or "cursor-generate-image",
            aspect_ratio=ratio,
        )
        saved_path = store.resolve_path(saved["id"])
        if saved_path is None:
            raise image_gen.ImageGenError("generated image was not persisted")
        response_b64 = base64.b64encode(saved_path.read_bytes()).decode("ascii")
        media_url = f"{public_base.rstrip('/')}/media/{saved['id']}" if public_base else f"/media/{saved['id']}"
        payload = image_gen.openai_images_response(
            b64_json=response_b64,
            created=saved.get("created"),
            url=media_url,
            response_format=response_format,
        )
        return payload

    def resolve_model(self, client_model: str | None) -> ModelSpec:
        """Resolve a client-facing model id to upstream id + params."""
        requested = (client_model or "").strip()
        if not requested:
            # Startup --model remains the default when the client omits model.
            try:
                base = self.catalogue.resolve(self.catalogue.default_alias)
            except ValueError:
                base = ModelSpec(
                    alias=self.options.model,
                    upstream_id=self.options.model,
                    params=[],
                )
            base.upstream_id = self.options.model
            return base
        return self.catalogue.resolve(requested)

    def _chat_mode(self) -> str:
        """agent (default) = GrokBotService; stream = InferenceService/Stream fallback."""
        mode = (
            getattr(self.options, "chat_mode", None)
            or os.environ.get("GROKBOT_CHAT_MODE")
            or "agent"
        )
        return str(mode).strip().lower() or "agent"

    def infer_via_grokbot_service(
        self,
        client_model: str,
        messages: list[Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        spec = self.resolve_model(client_model)
        meta = self.module.client_meta(self.args)
        machine_id = self.module.load_machine_id()
        agent_id = (
            getattr(self.options, "grokbot_agent_id", None)
            or os.environ.get("GROKBOT_AGENT_ID")
            or ""
        ).strip() or None
        agent_name = (
            getattr(self.options, "grokbot_agent_name", None)
            or os.environ.get("GROKBOT_AGENT_NAME")
            or grokbot_chat.DEFAULT_AGENT_NAME
        )
        with self.lock:
            session = self.session_token()
            try:
                result = grokbot_chat.chat_via_grokbot_service(
                    session_token=session,
                    backend_url=self.args.backend_url,
                    meta=meta,
                    machine_id=machine_id,
                    messages=messages,
                    agent_id=agent_id,
                    agent_name=str(agent_name),
                    timeout_ms=int(self.args.timeout_ms),
                    team_id=str(self.args.team_id) if self.args.team_id else None,
                )
            except grokbot_chat.GrokBotChatError as error:
                result = {
                    "ok": False,
                    "httpStatus": error.http_status or 502,
                    "text": "",
                    "tool_calls": [],
                    "error": str(error),
                    "transport": "GrokBotService/SendGrokBotUserMessage",
                    "debug": error.payload,
                }
        result = dict(result)
        result["clientModel"] = client_model
        result["resolvedUpstream"] = spec.upstream_id
        result["resolvedParams"] = [{"id": k, "value": v} for k, v in spec.params]
        result["retried"] = False
        result["retry_reason"] = ""
        # Keep catalogue model id for OpenAI response.model
        result["model"] = client_model or spec.alias or result.get("model")
        return result

    def infer_native(
        self,
        client_model: str,
        messages: list[Any],
        tools: list[Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        # Official Grok Bot chat uses GrokBotService, not InferenceService/Stream.
        # Stream still returns ERROR_NOT_HIGH_ENOUGH_PERMISSIONS on SuperGrok+Free.
        chat_mode = self._chat_mode()
        if chat_mode != "stream":
            # Agent path cannot pass OpenAI tools to GrokBotService. Clients like
            # Hermes often attach builtin tools even when streaming is off; calling
            # Stream solely because tools were present yields PERMISSIONS on many
            # accounts. Default: strip tools and stay on agent. Opt out with
            # GROKBOT_AGENT_STRIP_TOOLS=0 to restore Stream-when-tools behavior.
            if tools and not _agent_strip_tools_enabled():
                pass  # fall through to Stream (legacy / Stream-capable accounts)
            else:
                if tools:
                    print(
                        f"warning: chat_mode=agent stripping {len(tools)} client tool(s); "
                        f"GrokBotService has no Stream tool path. Set GROKBOT_AGENT_STRIP_TOOLS=0 "
                        f"to force InferenceService/Stream when tools are present.",
                        flush=True,
                    )
                return self.infer_via_grokbot_service(client_model, messages, request)

        with self.lock:
            # Multi-model: map client alias -> upstream model id + params.
            # Startup --model is only used when the client omits model (or as
            # catalogue fallback_upstream for unknown ids).
            spec = self.resolve_model(client_model)
            self.args.model = spec.upstream_id
            model_params = list(spec.params)
            self.ensure_renewal_credential()
            credential = self.module.load_renewal_credential(self.args)
            meta = self.module.client_meta(self.args)
            token = self.module.get_access_token(self.args, credential, meta)
            # getattr: callers may build an options namespace without the newer flag (the
            # project's own tests do), and the documented default is the safe behaviour.
            payload = clamp_output_ceiling(
                request, getattr(self.options, "min_max_tokens", DEFAULT_MIN_MAX_TOKENS)
            )
            result = native_stream_llm(
                self.module,
                self.args,
                token["accessToken"],
                messages,
                tools,
                payload,
                model_params=model_params,
            )
            retried = False
            retry_reason = ""
            if result.get("httpStatus") == 401:
                token = self.module.get_access_token(self.args, credential, meta, force=True)
                result = native_stream_llm(
                    self.module,
                    self.args,
                    token["accessToken"],
                    messages,
                    tools,
                    payload,
                    model_params=model_params,
                )
                retried = True
                retry_reason = "401-refresh"
            status = int(result.get("httpStatus") or 0)
            if status in TRANSIENT_UPSTREAM_STATUSES:
                time.sleep(RETRY_BACKOFF_SECONDS)
                result = native_stream_llm(
                    self.module,
                    self.args,
                    token["accessToken"],
                    messages,
                    tools,
                    payload,
                    model_params=model_params,
                )
                retried = True
                retry_reason = f"transient-{status}"
            result = dict(result)
            result["clientModel"] = client_model
            result["resolvedUpstream"] = spec.upstream_id
            result["resolvedParams"] = [{"id": k, "value": v} for k, v in model_params]
            result["retried"] = retried
            result["retry_reason"] = retry_reason
            return result

    def complete(
        self,
        model: str,
        messages: list[Any],
        tools: list[Any],
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, list[dict[str, Any]]]:
        result = self.infer_native(model, messages, tools, request)
        content = str(result.get("text", "")) or None
        calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
        return result, content, calls


class RequestStats:
    """In-memory request counters for the admin panel."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.total = 0
        self.success = 0
        self.errors = 0
        self.by_route: dict[str, int] = {}
        self.by_model: dict[str, int] = {}
        self.recent_errors: list[dict[str, Any]] = []

    def record(self, route: str, model: str = "", ok: bool = True, error: str = "") -> None:
        with self.lock:
            self.total += 1
            self.by_route[route] = self.by_route.get(route, 0) + 1
            if model:
                self.by_model[model] = self.by_model.get(model, 0) + 1
            if ok:
                self.success += 1
            else:
                self.errors += 1
                self.recent_errors.append(
                    {
                        "ts": int(time.time()),
                        "route": route,
                        "model": model,
                        "error": (error or "")[:500],
                    }
                )
                self.recent_errors = self.recent_errors[-50:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            total = self.total
            errors = self.errors
            return {
                "started_at": int(self.started_at),
                "uptime_seconds": int(time.time() - self.started_at),
                "total": total,
                "success": self.success,
                "errors": errors,
                "error_rate": round((errors / total), 4) if total else 0.0,
                "by_route": dict(self.by_route),
                "by_model": dict(self.by_model),
                "recent_errors": list(self.recent_errors[-20:]),
            }


class AuditLog:
    """In-memory ring buffer of recent inference / admin API requests."""

    def __init__(self, maxlen: int = AUDIT_RING_SIZE) -> None:
        self.lock = threading.Lock()
        self.maxlen = maxlen
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        *,
        path: str,
        model: str = "",
        status: int = 200,
        tokens: dict[str, Any] | None = None,
        error: str = "",
        retried: bool = False,
        retry_reason: str = "",
    ) -> None:
        entry = {
            "ts": int(time.time()),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "path": path,
            "model": model,
            "status": int(status),
            "tokens": tokens or {},
            "error": (error or "")[:500],
            "retried": bool(retried),
            "retry_reason": retry_reason or "",
        }
        with self.lock:
            self.entries.append(entry)
            if len(self.entries) > self.maxlen:
                self.entries = self.entries[-self.maxlen :]

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.entries[-max(1, min(limit, self.maxlen)) :])

    def snapshot(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_recent(limit)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        backend: Any,
        api_key: str,
        *,
        cors_origins: list[str] | None = None,
    ):
        super().__init__(address, ProxyHandler)
        self.backend = backend
        self.api_key = api_key
        self.response_history: dict[str, list[dict[str, Any]]] = {}
        self.response_history_order: list[str] = []
        self.response_history_lock = threading.Lock()
        self.stats = RequestStats()
        self.audit = AuditLog()
        self.cors_origins = list(cors_origins or [])
        self.started_at = time.time()
        self.version = __version__
        catalogue = getattr(backend, "catalogue", None)
        if catalogue is None:
            catalogue = ModelCatalogue(
                fallback_upstream=getattr(getattr(backend, "options", None), "model", "grok-4.7")
            )
        self.catalogue = catalogue
        self.listen_host = address[0]
        self.listen_port = address[1]
        store = getattr(backend, "media_store", None)
        self.media_store = store if store is not None else image_gen.MediaStore()

    def accepted_api_keys(self) -> set[str]:
        keys: set[str] = set()
        if self.api_key:
            keys.add(self.api_key)
        try:
            for value in self.catalogue.list_client_key_values():
                if value:
                    keys.add(value)
        except Exception:
            pass
        return keys

    def auth_required(self) -> bool:
        return bool(self.accepted_api_keys())

    def response_messages(self, response_id: Any) -> list[dict[str, Any]]:
        if not isinstance(response_id, str) or not response_id:
            return []
        with self.response_history_lock:
            return [dict(message) for message in self.response_history.get(response_id, [])]

    def remember_response(self, response_id: str, messages: list[dict[str, Any]]) -> None:
        with self.response_history_lock:
            self.response_history[response_id] = [dict(message) for message in messages]
            self.response_history_order.append(response_id)
            while len(self.response_history_order) > 128:
                expired = self.response_history_order.pop(0)
                self.response_history.pop(expired, None)



def admin_status_payload(server: "ProxyServer") -> dict[str, Any]:
    usage: dict[str, Any] | None = None
    usage_error = ""
    sand_usage = getattr(server.backend, "sand_usage", None)
    if callable(sand_usage):
        try:
            usage = sand_usage()
        except Exception as exc:  # pragma: no cover - live credential path
            usage_error = str(exc)
    catalogue = server.catalogue.snapshot()
    enabled_count = sum(1 for m in catalogue.get("models") or [] if m.get("enabled"))
    stats = server.stats.snapshot()
    media_count = 0
    try:
        store = getattr(server, "media_store", None)
        if store is not None:
            media_count = len(store.list_items(limit=1000))
    except Exception:
        media_count = 0
    return {
        "ok": True,
        "version": getattr(server, "version", __version__),
        "listen": f"http://{server.listen_host}:{server.server_port}",
        "health": "/health",
        "admin": "/admin",
        "docs": "/docs",
        "default_model": server.catalogue.default_alias,
        "startup_upstream_model": getattr(getattr(server.backend, "options", None), "model", None),
        "models_enabled": enabled_count,
        "catalogue": catalogue,
        "stats": stats,
        "audits": server.audit.snapshot(40),
        "usage": usage,
        "usage_error": usage_error,
        "api_key_required": server.auth_required(),
        "primary_api_key_configured": bool(server.api_key),
        "client_key_count": catalogue.get("client_key_count", 0),
        "cors_origins": list(server.cors_origins),
        "media_count": media_count,
        "uptime_seconds": stats.get("uptime_seconds", 0),
    }


def render_docs_html(server: "ProxyServer") -> str:
    listen = f"http://{server.listen_host}:{server.server_port}"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>grokbot2api docs</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#1a1a1a}}
code,pre{{background:#f4f4f5;border-radius:6px}} code{{padding:.1rem .35rem}} pre{{padding:1rem;overflow:auto}}
h1{{font-size:1.6rem}} h2{{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ddd;padding:.45rem .6rem;text-align:left;font-size:.92rem}}
.muted{{color:#666}}
</style></head><body>
<h1>grokbot2api API</h1>
<p class="muted">OpenAI-compatible + Anthropic Messages proxy to Cursor sand <code>InferenceService.Stream</code>.
Version <code>{getattr(server, "version", __version__)}</code> · base <code>{listen}</code></p>
<h2>Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Notes</th></tr>
<tr><td>GET</td><td><code>/health</code></td><td>Liveness; version, uptime, model count</td></tr>
<tr><td>GET</td><td><code>/docs</code></td><td>This page</td></tr>
<tr><td>GET</td><td><code>/v1/models</code></td><td>Catalogue (aliases)</td></tr>
<tr><td>GET</td><td><code>/v1/usage</code></td><td>Weekly sand allowance</td></tr>
<tr><td>POST</td><td><code>/v1/chat/completions</code></td><td>OpenAI Chat Completions + SSE</td></tr>
<tr><td>POST</td><td><code>/v1/responses</code></td><td>OpenAI Responses + SSE</td></tr>
<tr><td>POST</td><td><code>/v1/messages</code></td><td>Anthropic Messages + SSE (<code>/messages</code> alias)</td></tr>
<tr><td>POST</td><td><code>/v1/images/generations</code></td><td>OpenAI Images (AiService/RunGenerateImage)</td></tr>
<tr><td>GET</td><td><code>/media/&lt;id&gt;</code></td><td>Serve a saved generated image</td></tr>
<tr><td>GET</td><td><code>/admin</code></td><td>Full sidebar workbench</td></tr>
<tr><td>GET</td><td><code>/admin/api/audits</code></td><td>JSON export of recent audits</td></tr>
<tr><td>GET</td><td><code>/admin/api/media</code></td><td>Generated media gallery index</td></tr>
<tr><td>DELETE</td><td><code>/admin/api/media/&lt;id&gt;</code></td><td>Delete a saved media item</td></tr>
</table>
<h2>Auth</h2>
<p>When no keys are configured, loopback requests need no auth.
Otherwise send <code>Authorization: Bearer &lt;key&gt;</code> or <code>x-api-key: &lt;key&gt;</code>
(primary env key or any admin-managed client key).</p>
<h2>Anthropic example</h2>
<pre>curl -s {listen}/v1/messages \
  -H 'content-type: application/json' \
  -H 'x-api-key: YOUR_KEY' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{{"model":"cursor-grok-4-6","max_tokens":512,"messages":[{{"role":"user","content":"hi"}}]}}'</pre>
<p><a href="/admin">Admin</a> · <a href="/health">Health</a></p>
</body></html>
"""


ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>grokbot2api 登录</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f1419;color:#e7ecf3;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
card{background:#1a2332;padding:2rem;border-radius:12px;width:min(420px,92vw);box-shadow:0 8px 32px #0008}
h1{font-size:1.2rem;margin:0 0 1rem}
input,button{width:100%;padding:.7rem .8rem;margin:.4rem 0;border-radius:8px;border:1px solid #334;background:#0f1419;color:#e7ecf3;box-sizing:border-box}
button{background:#3b82f6;border:none;cursor:pointer;font-weight:600}
p{color:#9aa;font-size:.9rem}
</style></head><body><card>
<h1>grokbot2api 管理后台</h1>
<p>需要与代理相同的 API Key（Bearer）。未配置密钥时可直接打开 <code>/admin</code>。</p>
<input id="key" type="password" placeholder="API Key（主密钥或客户端密钥）"/>
<button onclick="login()">登录</button>
<p id="err" style="color:#f87171"></p>
<script>
function formatErr(v){
  if(v==null||v==='') return '';
  if(typeof v==='string') return v;
  if(v instanceof Error) return v.message||String(v);
  if(typeof v==='object'){
    if(typeof v.message==='string'&&v.message) return v.message;
    if(v.error!=null) return formatErr(v.error);
    try{return JSON.stringify(v);}catch(e){return String(v);}
  }
  return String(v);
}
async function login(){
  const api_key=document.getElementById('key').value;
  const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key})});
  let data={};
  try{data=await r.json();}catch(e){}
  if(!r.ok){document.getElementById('err').textContent=formatErr(data)||('登录失败 HTTP '+r.status);return;}
  const store=api_key||(data.key||'');
  if(store) localStorage.setItem('grokbot2api_key', store);
  location.href='/admin';
}
</script></card></body></html>
"""


def render_admin_html(server: "ProxyServer") -> str:
    """Full sidebar workbench (Chinese labels). Status loaded via /admin/api/status."""
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>grokbot2api 工作台</title>
<style>
:root{--bg:#0b1220;--panel:#111827;--card:#1a2332;--line:#243044;--text:#e7ecf3;--muted:#9aacbf;--accent:#3b82f6;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--chip:#0f172a}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:#93c5fd;text-decoration:none}a:hover{text-decoration:underline}
.layout{display:grid;grid-template-columns:230px 1fr;min-height:100vh}
.sidebar{background:var(--panel);border-right:1px solid var(--line);padding:1rem .75rem;display:flex;flex-direction:column;gap:.35rem}
.brand{padding:.4rem .65rem 1rem;font-weight:700;font-size:1.05rem}
.brand small{display:block;color:var(--muted);font-weight:500;margin-top:.25rem}
.navbtn{display:block;width:100%;text-align:left;background:transparent;border:1px solid transparent;color:var(--text);padding:.65rem .75rem;border-radius:10px;cursor:pointer;font-size:.95rem}
.navbtn:hover{background:#1e293b}.navbtn.active{background:#1e3a5f;border-color:#334155}
.content{display:flex;flex-direction:column;min-width:0}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.9rem 1.25rem;border-bottom:1px solid var(--line);background:#0f172aee;position:sticky;top:0;backdrop-filter:blur(6px);z-index:2;flex-wrap:wrap}
.topbar h1{margin:0;font-size:1.1rem}.topbar .row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
main{padding:1.1rem 1.25rem 2rem;display:none}main.active{display:block}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem 1.1rem}
.card h2{margin:0 0 .75rem;font-size:.92rem;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
.metric{font-size:1.55rem;font-weight:700}.metric small{font-size:.85rem;color:var(--muted);font-weight:500}
.kv{display:flex;justify-content:space-between;gap:1rem;padding:.35rem 0;border-bottom:1px solid #24304433;font-size:.92rem}.kv:last-child{border:none}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:.85rem}th,td{text-align:left;padding:.45rem .35rem;border-bottom:1px solid var(--line);vertical-align:top}
button,select{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.4rem .75rem;cursor:pointer}
button.secondary{background:#334155}button.danger{background:#b91c1c}button:disabled{opacity:.55;cursor:not-allowed}
input[type=text],input[type=password],input[type=number],textarea,select{background:#0f1419;border:1px solid #334;border-radius:8px;color:var(--text);padding:.45rem .6rem;width:100%;margin:.25rem 0}
textarea{min-height:110px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9rem}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.badge{display:inline-block;background:var(--chip);border:1px solid #334155;color:#cbd5e1;border-radius:999px;padding:.1rem .45rem;font-size:.72rem;margin-right:.2rem}
.badge.chat{color:#93c5fd}.badge.vision{color:#c4b5fd}.badge.image{color:#f9a8d4}.badge.custom{color:#fde68a}
#msg{min-height:1.2em;color:var(--ok);font-size:.9rem;padding:0 1.25rem 1rem}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.9rem}
.thumb{background:#0f1419;border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.thumb img{width:100%;aspect-ratio:1;object-fit:cover;background:#000;display:block}
.thumb .meta{padding:.6rem .7rem;font-size:.8rem;color:var(--muted);display:flex;flex-direction:column;gap:.25rem}
.pre{white-space:pre-wrap;background:#0f1419;border:1px solid var(--line);border-radius:10px;padding:.75rem;max-height:320px;overflow:auto;font-size:.85rem}
.form-grid{display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.muted{color:var(--muted);font-size:.9rem}
@media (max-width:860px){.layout{grid-template-columns:1fr}.sidebar{flex-direction:row;flex-wrap:wrap;border-right:none;border-bottom:1px solid var(--line)}.brand{width:100%}.navbtn{width:auto}}
</style></head><body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">grokbot2api 工作台<small id="ver"></small></div>
    <button class="navbtn active" data-sec="overview" onclick="showSec('overview')">总览</button>
    <button class="navbtn" data-sec="models" onclick="showSec('models')">模型</button>
    <button class="navbtn" data-sec="keys" onclick="showSec('keys')">密钥</button>
    <button class="navbtn" data-sec="audits" onclick="showSec('audits')">审计</button>
    <button class="navbtn" data-sec="media" onclick="showSec('media')">媒体</button>
    <button class="navbtn" data-sec="playground" onclick="showSec('playground')">试用</button>
    <button class="navbtn" data-sec="settings" onclick="showSec('settings')">设置</button>
  </aside>
  <div class="content">
    <div class="topbar">
      <h1 id="secTitle">总览</h1>
      <div class="row">
        <a href="/docs">API 文档</a>
        <a href="/health">Health</a>
        <button class="secondary" onclick="refreshAll()">刷新</button>
        <button class="secondary" onclick="logout()">退出</button>
      </div>
    </div>

    <main id="overview" class="active">
      <div class="grid" id="overviewCards"></div>
      <div class="card" style="margin-top:1rem" id="usageCard"></div>
      <div class="card" style="margin-top:1rem" id="recentErrorsCard"></div>
    </main>

    <main id="models">
      <div class="card">
        <h2>模型目录</h2>
        <p class="muted">启用/禁用、设默认，并可为内置目录添加自定义别名（写入 admin_config.json）。</p>
        <div style="overflow:auto"><table>
          <thead><tr><th>启用</th><th>别名</th><th>能力</th><th>上游 ID</th><th>参数</th><th>默认</th></tr></thead>
          <tbody id="modelsBody"></tbody>
        </table></div>
      </div>
      <div class="card" style="margin-top:1rem">
        <h2>添加自定义别名</h2>
        <div class="form-grid">
          <div><label class="muted">别名</label><input id="cmAlias" type="text" placeholder="my-grok-fast"/></div>
          <div><label class="muted">上游 ID</label><input id="cmUpstream" type="text" placeholder="grok-4.7"/></div>
          <div><label class="muted">显示名</label><input id="cmDisplay" type="text" placeholder="可选"/></div>
          <div><label class="muted">参数（key=value, 逗号分隔）</label><input id="cmParams" type="text" placeholder="effort=high, fast=true"/></div>
          <div><label class="muted">能力</label>
            <div class="row" style="margin-top:.4rem">
              <label><input type="checkbox" id="cmChat" checked/> chat</label>
              <label><input type="checkbox" id="cmVision"/> vision</label>
              <label><input type="checkbox" id="cmImage"/> image_generation</label>
            </div>
          </div>
        </div>
        <div class="row" style="margin-top:.8rem"><button onclick="addCustomModel()">添加别名</button></div>
      </div>
    </main>

    <main id="keys">
      <div class="card">
        <h2>客户端 API Keys</h2>
        <p class="muted">任意已登记密钥或主环境变量密钥均可鉴权。界面永不展示完整主密钥或沙箱凭证。</p>
        <div class="row" style="margin-bottom:.8rem">
          <input id="newKey" type="text" placeholder="新密钥（可留空自动生成，≥8 字符）" style="flex:1;min-width:180px"/>
          <input id="newKeyName" type="text" placeholder="备注名（可选）" style="flex:1;min-width:120px"/>
          <button onclick="createKey()">创建</button>
        </div>
        <table><thead><tr><th>备注</th><th>预览</th><th>创建时间</th><th></th></tr></thead>
        <tbody id="keysBody"></tbody></table>
      </div>
    </main>

    <main id="audits">
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:.6rem">
          <h2 style="margin:0">请求审计</h2>
          <button class="secondary" onclick="exportAudits()">导出 JSON</button>
        </div>
        <div style="overflow:auto"><table>
          <thead><tr><th>时间</th><th>路径</th><th>模型</th><th>状态</th><th>Tokens</th><th>重试</th><th>错误</th></tr></thead>
          <tbody id="auditsBody"></tbody>
        </table></div>
      </div>
    </main>

    <main id="media">
      <div class="card">
        <div class="row" style="justify-content:space-between;margin-bottom:.6rem">
          <h2 style="margin:0">媒体库</h2>
          <button class="secondary" onclick="loadMedia()">刷新媒体</button>
        </div>
        <p class="muted">来自 media/ 索引；缩略图经 /media/&lt;id&gt; 加载。</p>
        <div class="gallery" id="mediaGallery"></div>
      </div>
    </main>

    <main id="playground">
      <div class="grid">
        <div class="card">
          <h2>Chat 试用</h2>
          <label class="muted">模型</label>
          <select id="pgChatModel"></select>
          <label class="muted">消息</label>
          <textarea id="pgChatPrompt" placeholder="你好，请用一句话介绍自己"></textarea>
          <div class="row" style="margin-top:.5rem"><button onclick="runChat()">发送 /v1/chat/completions</button></div>
          <div class="pre" id="pgChatOut" style="margin-top:.75rem">等待输出…</div>
        </div>
        <div class="card">
          <h2>图像生成试用</h2>
          <label class="muted">模型</label>
          <select id="pgImgModel"><option value="cursor-generate-image">cursor-generate-image</option></select>
          <label class="muted">Prompt</label>
          <textarea id="pgImgPrompt" placeholder="a watercolor fox under moonlight"></textarea>
          <label class="muted">aspect_ratio</label>
          <select id="pgAspect">
            <option>1:1</option><option>4:3</option><option>3:4</option><option>16:9</option><option>9:16</option>
          </select>
          <div class="row" style="margin-top:.5rem"><button onclick="runImage()">生成 /v1/images/generations</button></div>
          <div id="pgImgOut" style="margin-top:.75rem" class="muted">等待图像…</div>
        </div>
      </div>
    </main>

    <main id="settings">
      <div class="card" id="settingsCard"></div>
      <div class="card" style="margin-top:1rem">
        <h2>说明</h2>
        <p class="muted">CORS 与监听地址由启动参数决定（--cors-origins / --listen / --port），此处只读展示。密钥与 Cursor 凭证永不在本页明文显示。</p>
      </div>
    </main>
    <p id="msg"></p>
  </div>
</div>
<script>
const TITLES={overview:'总览',models:'模型',keys:'密钥',audits:'审计',media:'媒体',playground:'试用',settings:'设置'};
let STATE=null;
const key=()=>localStorage.getItem('grokbot2api_key')||'';
function authHeaders(extra){const h=Object.assign({'Content-Type':'application/json'},extra||{}); const k=key(); if(k) h['Authorization']='Bearer '+k; return h;}
function formatErr(v){
  if(v==null||v==='') return '';
  if(typeof v==='string') return v;
  if(v instanceof Error) return v.message||String(v);
  if(typeof v==='object'){
    if(typeof v.message==='string'&&v.message) return v.message;
    if(v.error!=null) return formatErr(v.error);
    try{return JSON.stringify(v);}catch(e){return Object.prototype.toString.call(v);}
  }
  return String(v);
}
async function readJsonSafe(r){try{return await r.json();}catch(e){return {message:'HTTP '+r.status+'（响应非 JSON）'};}}
function logout(){localStorage.removeItem('grokbot2api_key'); location.href='/admin/login';}
function pct(n){return ((n||0)*100).toFixed(1)+'%';}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function showSec(id, syncHash){
  if(!TITLES[id]) id='overview';
  document.querySelectorAll('main').forEach(el=>el.classList.toggle('active', el.id===id));
  document.querySelectorAll('.navbtn').forEach(el=>el.classList.toggle('active', el.dataset.sec===id));
  document.getElementById('secTitle').textContent=TITLES[id]||id;
  if(syncHash!==false){
    const want=id==='overview'?'':id;
    if((location.hash||'').replace(/^#/,'')!==want){
      history.replaceState(null,'', want?('#'+want):location.pathname+location.search);
    }
  }
  if(id==='media') loadMedia();
}
function applyHashRoute(){
  const h=(location.hash||'').replace(/^#/,'').trim();
  showSec(h && TITLES[h]?h:'overview', false);
}
window.addEventListener('hashchange', applyHashRoute);
function capsBadges(m){
  const caps=m.capabilities||[];
  let html='';
  if(caps.includes('chat')||(!caps.includes('image_generation') && m.supports_vision!==false)) html+='<span class="badge chat">chat</span>';
  if(m.supports_vision || caps.includes('vision')) html+='<span class="badge vision">vision</span>';
  if(caps.includes('image_generation')) html+='<span class="badge image">image-gen</span>';
  if(m.is_custom) html+='<span class="badge custom">custom</span>';
  return html||'—';
}
function renderOverview(s){
  const stats=s.stats||{}; const usage=s.usage||{};
  document.getElementById('overviewCards').innerHTML=`
    <div class="card"><h2>版本 / 运行</h2><div class="metric">v${esc(s.version||'')}</div>
      <div class="kv"><span>Uptime</span><span>${stats.uptime_seconds??s.uptime_seconds??0}s</span></div>
      <div class="kv"><span>监听</span><span><code>${esc(s.listen||'')}</code></span></div></div>
    <div class="card"><h2>模型</h2><div class="metric">${s.models_enabled??0} <small>/ ${((s.catalogue||{}).models||[]).length}</small></div>
      <div class="kv"><span>默认别名</span><span><code>${esc(s.default_model||'')}</code></span></div>
      <div class="kv"><span>上游默认</span><span><code>${esc(s.startup_upstream_model||'')}</code></span></div></div>
    <div class="card"><h2>请求量</h2><div class="metric">${stats.total||0}</div>
      <div class="kv"><span>成功</span><span class="ok">${stats.success||0}</span></div>
      <div class="kv"><span>失败</span><span class="${(stats.errors||0)?'bad':'ok'}">${stats.errors||0}</span></div></div>
    <div class="card"><h2>错误率</h2><div class="metric ${(stats.error_rate||0)>0.05?'bad':'ok'}">${pct(stats.error_rate||0)}</div>
      <div class="kv"><span>媒体数</span><span>${s.media_count??0}</span></div>
      <div class="kv"><span>链接</span><span><a href="/docs">/docs</a> · <a href="/health">/health</a></span></div></div>`;
  document.getElementById('usageCard').innerHTML=`<h2>用量 /v1/usage</h2>${
    s.usage_error?`<div class="bad">${esc(s.usage_error)}</div>`:`
    <div class="kv"><span>已用</span><span>${usage.usagePercent??'—'}%</span></div>
    <div class="kv"><span>剩余</span><span>${usage.usageRemainingPercent??'—'}%</span></div>
    <div class="kv"><span>套餐</span><span>${esc(usage.grokPlanLabel||usage.cursorPlanName||'—')}</span></div>
    <div class="kv"><span>重置</span><span>${esc(usage.nextResetTimestampUtc||'—')}</span></div>`}`;
  const errs=stats.recent_errors||[];
  document.getElementById('recentErrorsCard').innerHTML=`<h2>最近错误</h2><table><thead><tr><th>时间</th><th>路由</th><th>模型</th><th>错误</th></tr></thead><tbody>${
    errs.length? errs.slice().reverse().map(e=>`<tr><td>${new Date((e.ts||0)*1000).toLocaleString()}</td><td>${esc(e.route||'')}</td><td>${esc(e.model||'')}</td><td class="bad">${esc(e.error||'')}</td></tr>`).join('')
    : '<tr><td colspan="4">暂无</td></tr>'}</tbody></table>`;
}
function renderModels(s){
  const models=((s.catalogue||{}).models||[]).slice().sort((a,b)=>String(a.id).localeCompare(String(b.id)));
  document.getElementById('modelsBody').innerHTML=models.map(m=>{
    const params=(m.params||[]).map(p=>`${p.id}=${p.value}`).join(', ')||'—';
    const isDefault=m.id===s.default_model;
    return `<tr>
      <td><input type="checkbox" data-alias="${esc(m.id)}" ${m.enabled?'checked':''} onchange="toggle(this)"/></td>
      <td><code>${esc(m.id)}</code><div class="muted">${esc(m.display_name||'')}</div></td>
      <td>${capsBadges(m)}</td>
      <td><code>${esc(m.upstream_id||'')}</code></td>
      <td>${esc(params)}</td>
      <td>${isDefault?'✓':`<button class="secondary" onclick="setDefault('${esc(m.id)}')">设为默认</button>`}</td>
    </tr>`;
  }).join('')||'<tr><td colspan="6">暂无模型</td></tr>';
  const chatSel=document.getElementById('pgChatModel');
  const imgSel=document.getElementById('pgImgModel');
  const chatModels=models.filter(m=>m.enabled && ((m.capabilities||[]).includes('chat') || !(m.capabilities||[]).includes('image_generation')));
  const imgModels=models.filter(m=>m.enabled && (m.capabilities||[]).includes('image_generation'));
  chatSel.innerHTML=chatModels.map(m=>`<option value="${esc(m.id)}" ${m.id===s.default_model?'selected':''}>${esc(m.id)}</option>`).join('')||'<option value="cursor-grok-4-7">cursor-grok-4-7</option>';
  imgSel.innerHTML=imgModels.map(m=>`<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('')||'<option value="cursor-generate-image">cursor-generate-image</option>';
}
function renderKeys(s){
  const keys=((s.catalogue||{}).client_keys||[]);
  document.getElementById('keysBody').innerHTML=keys.length? keys.map(k=>`<tr>
    <td>${esc(k.name||'—')}</td><td><code>${esc(k.key_preview||'')}</code></td>
    <td>${k.created_at? new Date(k.created_at*1000).toLocaleString():'—'}</td>
    <td><button class="danger" onclick="revokeKey('${esc(k.id)}')">吊销</button></td>
  </tr>`).join('') : '<tr><td colspan="4">暂无客户端密钥</td></tr>';
}
function renderAudits(s){
  const audits=s.audits||[];
  document.getElementById('auditsBody').innerHTML=audits.length? audits.slice().reverse().map(e=>{
    const tok=e.tokens||{};
    const tokS=(tok.prompt_tokens!=null||tok.completion_tokens!=null)? `${tok.prompt_tokens||0}/${tok.completion_tokens||0}` : '—';
    return `<tr>
      <td>${esc(e.time|| (e.ts? new Date(e.ts*1000).toLocaleString():''))}</td>
      <td><code>${esc(e.path||'')}</code></td><td>${esc(e.model||'')}</td>
      <td class="${(e.status||0)>=400?'bad':'ok'}">${e.status||''}</td>
      <td>${tokS}</td><td>${e.retried? esc(e.retry_reason||'是'):'—'}</td>
      <td class="bad">${esc(e.error||'')}</td></tr>`;
  }).join('') : '<tr><td colspan="7">暂无</td></tr>';
}
function renderSettings(s){
  document.getElementById('settingsCard').innerHTML=`<h2>运行设置（只读）</h2>
    <div class="kv"><span>需要 API Key</span><span>${s.api_key_required?'是':'否'}</span></div>
    <div class="kv"><span>主密钥(环境变量)</span><span>${s.primary_api_key_configured?'已配置':'未配置'}</span></div>
    <div class="kv"><span>客户端密钥数</span><span>${s.client_key_count||0}</span></div>
    <div class="kv"><span>CORS</span><span><code>${esc((s.cors_origins||[]).join(', ')||'关闭')}</code></span></div>
    <div class="kv"><span>监听</span><span><code>${esc(s.listen||'')}</code></span></div>
    <div class="kv"><span>文档</span><span><a href="/docs">/docs</a></span></div>
    <div class="kv"><span>健康检查</span><span><a href="/health">/health</a></span></div>`;
}
function render(s){
  STATE=s;
  document.getElementById('ver').textContent='v'+(s.version||'');
  renderOverview(s); renderModels(s); renderKeys(s); renderAudits(s); renderSettings(s);
}
async function refreshAll(){
  const r=await fetch('/admin/api/status',{headers:authHeaders()});
  if(r.status===401){location.href='/admin/login';return;}
  const s=await r.json(); render(s);
  const active=document.querySelector('main.active');
  if(active && active.id==='media') loadMedia();
}
async function toggle(el){
  const body={enabled:{[el.dataset.alias]: el.checked}};
  const r=await fetch('/admin/api/models',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
  document.getElementById('msg').textContent=r.ok?'已保存':'保存失败';
  if(r.ok) render(await r.json());
}
async function setDefault(alias){
  const r=await fetch('/admin/api/models',{method:'POST',headers:authHeaders(),body:JSON.stringify({default_alias:alias})});
  document.getElementById('msg').textContent=r.ok?'默认模型已更新':'更新失败';
  if(r.ok) render(await r.json());
}
async function addCustomModel(){
  const caps=[];
  if(document.getElementById('cmChat').checked) caps.push('chat');
  if(document.getElementById('cmVision').checked) caps.push('vision');
  if(document.getElementById('cmImage').checked) caps.push('image_generation');
  const params=[];
  const raw=(document.getElementById('cmParams').value||'').trim();
  if(raw){
    for(const part of raw.split(',')){
      const [k,...rest]=part.split('=');
      if(!k||!rest.length) continue;
      params.push({id:k.trim(), value:rest.join('=').trim()});
    }
  }
  const body={
    action:'add',
    alias:document.getElementById('cmAlias').value,
    upstream_id:document.getElementById('cmUpstream').value,
    display_name:document.getElementById('cmDisplay').value,
    params, capabilities:caps,
    supports_vision:document.getElementById('cmVision').checked
  };
  const r=await fetch('/admin/api/models',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
  const data=await r.json();
  document.getElementById('msg').textContent=r.ok?'自定义别名已添加':('失败: '+formatErr(data));
  if(r.ok){['cmAlias','cmUpstream','cmDisplay','cmParams'].forEach(id=>document.getElementById(id).value=''); render(data);}
}
async function createKey(){
  const keyVal=document.getElementById('newKey').value;
  const name=document.getElementById('newKeyName').value;
  const r=await fetch('/admin/api/keys',{method:'POST',headers:authHeaders(),body:JSON.stringify({action:'create',key:keyVal,name})});
  const body=await readJsonSafe(r);
  if(!r.ok){
    const detail=formatErr(body)||('HTTP '+r.status);
    document.getElementById('msg').textContent=r.status===401
      ? ('创建失败: 需要登录（'+detail+'）。请先到登录页填写主密钥或已有客户端密钥。')
      : ('创建失败: '+detail);
    if(r.status===401){setTimeout(()=>location.href='/admin/login',800);}
    return;
  }
  const created=(body.key&&body.key.key)||'';
  if(created && !key()) localStorage.setItem('grokbot2api_key', created);
  document.getElementById('msg').textContent=created
    ? ('密钥已创建：'+created+'（请妥善保存；已写入本页鉴权）')
    : '密钥已创建';
  document.getElementById('newKey').value=''; document.getElementById('newKeyName').value='';
  if(body.status) render(body.status); else refreshAll();
}
async function revokeKey(id){
  if(!confirm('确认吊销该密钥？')) return;
  const r=await fetch('/admin/api/keys',{method:'POST',headers:authHeaders(),body:JSON.stringify({action:'revoke',id})});
  document.getElementById('msg').textContent=r.ok?'已吊销':('吊销失败: '+formatErr(await readJsonSafe(r)));
  if(r.ok) refreshAll();
}
async function exportAudits(){
  const r=await fetch('/admin/api/audits?limit=500',{headers:authHeaders()});
  if(!r.ok){document.getElementById('msg').textContent='导出失败';return;}
  const data=await r.json();
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='grokbot2api-audits.json'; a.click();
}
async function loadMedia(){
  const box=document.getElementById('mediaGallery');
  box.innerHTML='<div class="muted">加载中…</div>';
  const r=await fetch('/admin/api/media',{headers:authHeaders()});
  if(r.status===401){location.href='/admin/login';return;}
  if(!r.ok){box.innerHTML='<div class="bad">加载失败</div>';return;}
  const data=await r.json();
  const items=data.items||[];
  if(!items.length){box.innerHTML='<div class="muted">暂无生成图片</div>';return;}
  box.innerHTML=items.map(it=>`<div class="thumb">
    <a href="/media/${esc(it.id)}" target="_blank" rel="noopener"><img src="/media/${esc(it.id)}" alt="${esc(it.prompt||it.id)}"/></a>
    <div class="meta">
      <div><code>${esc(it.id).slice(0,10)}…</code></div>
      <div>${esc(it.model||'—')} · ${esc(it.aspect_ratio||'')}</div>
      <div>${it.created? new Date(it.created*1000).toLocaleString():'—'}</div>
      <div title="${esc(it.prompt||'')}">${esc((it.prompt||'').slice(0,80))||'—'}</div>
      <button class="danger" onclick="deleteMedia('${esc(it.id)}')">删除</button>
    </div></div>`).join('');
}
async function deleteMedia(id){
  if(!confirm('删除该媒体？')) return;
  const r=await fetch('/admin/api/media/'+encodeURIComponent(id),{method:'DELETE',headers:authHeaders()});
  document.getElementById('msg').textContent=r.ok?'已删除':'删除失败';
  if(r.ok){loadMedia(); refreshAll();}
}
async function runChat(){
  const out=document.getElementById('pgChatOut');
  out.textContent='请求中…';
  const payload={model:document.getElementById('pgChatModel').value, messages:[{role:'user',content:document.getElementById('pgChatPrompt').value||'hi'}], stream:false};
  try{
    const r=await fetch('/v1/chat/completions',{method:'POST',headers:authHeaders(),body:JSON.stringify(payload)});
    const data=await readJsonSafe(r);
    if(!r.ok){out.textContent='错误 '+r.status+': '+formatErr(data);return;}
    const text=(((data.choices||[])[0]||{}).message||{}).content||JSON.stringify(data,null,2);
    out.textContent=text;
  }catch(e){out.textContent=formatErr(e);}
}
async function runImage(){
  const out=document.getElementById('pgImgOut');
  out.innerHTML='生成中…';
  const payload={model:document.getElementById('pgImgModel').value, prompt:document.getElementById('pgImgPrompt').value||'a fox', aspect_ratio:document.getElementById('pgAspect').value, response_format:'url'};
  try{
    const r=await fetch('/v1/images/generations',{method:'POST',headers:authHeaders(),body:JSON.stringify(payload)});
    const data=await r.json();
    if(!r.ok){out.innerHTML='<div class="bad">错误 '+r.status+': '+esc(JSON.stringify(data))+'</div>';return;}
    const url=(((data.data||[])[0]||{}).url)||'';
    const b64=(((data.data||[])[0]||{}).b64_json)||'';
    if(url) out.innerHTML=`<a href="${esc(url)}" target="_blank"><img src="${esc(url)}" style="max-width:100%;border-radius:10px;border:1px solid #243044"/></a><div class="muted" style="margin-top:.4rem">${esc(url)}</div>`;
    else if(b64) out.innerHTML=`<img src="data:image/png;base64,${b64}" style="max-width:100%;border-radius:10px;border:1px solid #243044"/>`;
    else out.textContent=JSON.stringify(data,null,2);
    loadMedia();
  }catch(e){out.innerHTML='<div class="bad">'+esc(e)+'</div>';}
}
applyHashRoute(); refreshAll();
setInterval(refreshAll, 20000);
</script></body></html>
"""


class ProxyHandler(MessagesApiMixin, ResponsesApiMixin, BaseHTTPRequestHandler):
    server: ProxyServer
    protocol_version = "HTTP/1.1"
    stream_heartbeat_seconds = STREAM_HEARTBEAT_SECONDS

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def add_cors_headers(self) -> None:
        origins = getattr(self.server, "cors_origins", None) or []
        if not origins:
            return
        request_origin = self.headers.get("Origin", "")
        if "*" in origins:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif request_origin and request_origin in origins:
            self.send_header("Access-Control-Allow-Origin", request_origin)
            self.send_header("Vary", "Origin")
        elif origins and not request_origin:
            # Non-browser clients: echo first configured origin.
            self.send_header("Access-Control-Allow-Origin", origins[0])
        else:
            return
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, anthropic-version")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.add_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise ClientDisconnected from exc

    def _presented_api_key(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for header in ("X-Api-Key", "x-api-key"):
            value = self.headers.get(header, "")
            if value:
                return value.strip()
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "grokbot2api_key" and value:
                return value.strip()
        return ""

    def authorized(self) -> bool:
        accepted = self.server.accepted_api_keys()
        if not accepted:
            return True
        presented = self._presented_api_key()
        return bool(presented) and presented in accepted

    def send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.add_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise ClientDisconnected from exc

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.add_cors_headers()
        # Always advertise CORS methods on preflight even if Origin mismatched,
        # so browsers can surface a clear error.
        if not getattr(self.server, "cors_origins", None):
            pass
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in {"/", "/health"}:
            catalogue = self.server.catalogue.snapshot()
            enabled = sum(1 for m in catalogue.get("models") or [] if m.get("enabled"))
            stats = self.server.stats.snapshot()
            renewal_ok = bool(getattr(getattr(self.server, "backend", None), "renewal_configured", True))
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "grokbot2api",
                    "version": getattr(self.server, "version", __version__),
                    "uptime_seconds": stats.get("uptime_seconds", 0),
                    "models_enabled": enabled,
                    "models_total": len(catalogue.get("models") or []),
                    "default_model": catalogue.get("default_alias") or self.server.catalogue.default_alias,
                    "renewal_configured": renewal_ok,
                    "docs": "/docs",
                    "admin": "/admin",
                },
            )
            return
        if path in {"/docs"}:
            self.send_html(200, render_docs_html(self.server))
            return
        # Admin HTML shell is public so Tauri iframe / localStorage Bearer auth works
        # without relying on HttpOnly cookies (often blocked as third-party in WebView).
        if path in {"/admin/login", "/dashboard/login"}:
            self.send_html(200, ADMIN_LOGIN_HTML)
            return
        if path in {"/admin", "/dashboard"}:
            self.send_html(200, render_admin_html(self.server))
            return
        # Every other route describes the account (model catalogue, allowance), so it carries the
        # same Bearer token as inference. Only the health probe, docs, and admin HTML stay open.
        if not self.authorized():
            self.send_json(401, api_error_payload("无效的 API Key，请先登录或在请求头携带 Bearer / x-api-key", "authentication_error"))
            return
        if path in {"/admin/api/status", "/dashboard/api/status"}:
            self.send_json(200, admin_status_payload(self.server))
            return
        if path in {"/admin/api/audits", "/dashboard/api/audits"}:
            limit = 100
            query = self.path.split("?", 1)
            if len(query) == 2:
                for part in query[1].split("&"):
                    name, _, value = part.partition("=")
                    if name == "limit":
                        try:
                            limit = int(value)
                        except ValueError:
                            pass
            self.send_json(
                200,
                {
                    "ok": True,
                    "count": len(self.server.audit.list_recent(limit)),
                    "audits": self.server.audit.list_recent(limit),
                },
            )
            return
        if path in {"/admin/api/media", "/dashboard/api/media"}:
            limit = 200
            query = self.path.split("?", 1)
            if len(query) == 2:
                for part in query[1].split("&"):
                    name, _, value = part.partition("=")
                    if name == "limit":
                        try:
                            limit = int(value)
                        except ValueError:
                            pass
            store = getattr(self.server, "media_store", None) or image_gen.MediaStore()
            items = store.list_items(limit=limit)
            self.send_json(200, {"ok": True, "count": len(items), "items": items})
            return
        if path in {"/usage", "/v1/usage"}:
            # Weekly sand allowance, the same figure the desktop client shows as "Weekly usage".
            try:
                payload = self.server.backend.sand_usage()
            except Exception as exc:
                payload = {"error": str(exc)}
            self.send_json(200, payload)
            return
        if path.startswith("/media/"):
            media_id = path[len("/media/") :].strip("/")
            if not media_id or "/" in media_id or ".." in media_id:
                self.send_json(404, api_error_payload("not found", "invalid_request_error"))
                return
            store = getattr(self.server, "media_store", None) or image_gen.MediaStore()
            item = store.get(media_id)
            file_path = store.resolve_path(media_id)
            if not item or file_path is None:
                self.send_json(404, api_error_payload("media not found", "invalid_request_error"))
                return
            raw = file_path.read_bytes()
            mime = str(item.get("mime_type") or "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.add_cors_headers()
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in {"/v1/models", "/models"}:
            data = self.server.catalogue.list_public(only_enabled=True)
            # OpenAI-shaped list; keep id = client alias.
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": item["id"],
                            "object": "model",
                            "owned_by": item.get("owned_by", "local-sand-adapter"),
                            "upstream_id": item.get("upstream_id"),
                            "params": item.get("params"),
                            "context_window": item.get("context_window"),
                            "supports_vision": item.get("supports_vision", True),
                            "capabilities": item.get("capabilities") or ["chat"],
                        }
                        for item in data
                    ],
                    "default": self.server.catalogue.default_alias,
                },
            )
            return
        self.send_json(404, api_error_payload("not found", "invalid_request_error"))

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self.authorized():
            self.send_json(401, api_error_payload("无效的 API Key，请先登录或在请求头携带 Bearer / x-api-key", "authentication_error"))
            return
        prefixes = ("/admin/api/media/", "/dashboard/api/media/")
        matched = next((prefix for prefix in prefixes if path.startswith(prefix)), "")
        if not matched:
            self.send_json(404, api_error_payload("not found", "invalid_request_error"))
            return
        media_id = path[len(matched) :].strip("/")
        if not media_id or "/" in media_id or ".." in media_id:
            self.send_json(400, api_error_payload("invalid media id", "invalid_request_error"))
            return
        store = getattr(self.server, "media_store", None) or image_gen.MediaStore()
        try:
            store.delete(media_id)
        except KeyError:
            self.send_json(404, api_error_payload("media not found", "invalid_request_error"))
            return
        except ValueError as exc:
            self.send_json(400, api_error_payload(str(exc), "invalid_request_error"))
            return
        self.send_json(200, {"ok": True, "deleted": media_id, "media_count": len(store.list_items(limit=1000))})


    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in {"/admin/api/login", "/dashboard/api/login"}:
            self.handle_admin_login()
            return
        if not self.authorized():
            self.send_json(401, api_error_payload("无效的 API Key，请先登录或在请求头携带 Bearer / x-api-key", "authentication_error"))
            return
        if path in {"/admin/api/models", "/dashboard/api/models"}:
            self.handle_admin_models_update()
            return
        if path in {"/admin/api/keys", "/dashboard/api/keys"}:
            self.handle_admin_keys()
            return
        images_path = path in {"/v1/images/generations", "/images/generations"}
        chat_path = path in {"/v1/chat/completions", "/chat/completions"}
        responses_path = path in {"/v1/responses", "/responses"}
        messages_path = path in {"/v1/messages", "/messages"}
        if not chat_path and not responses_path and not messages_path and not images_path:
            self.send_json(
                404,
                api_error_payload(
                    "supported: /v1/chat/completions, /v1/responses, "
                    "/v1/messages, /v1/images/generations",
                    "invalid_request_error",
                ),
            )
            return
        if images_path:
            route = "/v1/images/generations"
            model_for_stats = ""
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_REQUEST_BYTES:
                    raise ValueError("invalid Content-Length")
                raw = self.rfile.read(size)
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("JSON object required")
                model_for_stats = str(request.get("model") or "cursor-generate-image")
                self.handle_image_generations(request)
                self.server.stats.record(route, model_for_stats, ok=True)
                self.server.audit.record(
                    path=route,
                    model=model_for_stats,
                    status=200,
                    error="",
                )
            except ValueError as exc:
                self.server.stats.record(route, model_for_stats, ok=False, error=str(exc))
                self.server.audit.record(path=route, model=model_for_stats, status=400, error=str(exc))
                self.send_json(400, api_error_payload(str(exc), "invalid_request_error"))
            except image_gen.ImageGenError as exc:
                self.server.stats.record(route, model_for_stats, ok=False, error=str(exc))
                status = 502 if not exc.http_status or exc.http_status == 200 else min(exc.http_status, 599)
                if status < 400:
                    status = 502
                self.server.audit.record(path=route, model=model_for_stats, status=status, error=str(exc))
                self.send_json(
                    status,
                    api_error_payload(
                        str(exc),
                        "upstream_error",
                        model_restricted=exc.model_restricted,
                        content_safety_blocked=exc.content_safety_blocked,
                    ),
                )
            except Exception as exc:
                self.server.stats.record(route, model_for_stats, ok=False, error=str(exc))
                self.server.audit.record(path=route, model=model_for_stats, status=502, error=str(exc))
                self.send_json(502, api_error_payload(str(exc), "upstream_error"))
            return
        if chat_path:
            route = "/v1/chat/completions"
        elif responses_path:
            route = "/v1/responses"
        else:
            route = "/v1/messages"
        model_for_stats = ""
        self._last_usage = {}
        self._last_retried = False
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("invalid or oversized request body")
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("request body must be an object")
            model_for_stats = str(request.get("model") or "")
            if chat_path:
                if not isinstance(request.get("messages"), list):
                    raise ValueError("messages must be an array")
                self.handle_completion(request)
            elif messages_path:
                self.handle_messages(request)
            else:
                self.handle_response(request)
            self.server.stats.record(route, model_for_stats, ok=True)
            self.server.audit.record(
                path=route,
                model=model_for_stats,
                status=200,
                tokens=getattr(self, "_last_usage", {}) or {},
                retried=bool(getattr(self, "_last_retried", False)),
                retry_reason=str(getattr(self, "_last_retry_reason", "") or ""),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self.server.stats.record(route, model_for_stats, ok=False, error=str(exc))
            self.server.audit.record(path=route, model=model_for_stats, status=400, error=str(exc))
            try:
                self.send_json(400, api_error_payload(str(exc), "invalid_request_error"))
            except ClientDisconnected:
                pass
        except ClientDisconnected:
            self.log_message("client disconnected; response abandoned")
        except Exception as exc:
            self.server.stats.record(route, model_for_stats, ok=False, error=str(exc))
            self.server.audit.record(path=route, model=model_for_stats, status=502, error=str(exc))
            traceback.print_exc(file=sys.stderr)
            try:
                self.send_json(502, api_error_payload(str(exc), "upstream_error"))
            except ClientDisconnected:
                pass

    def handle_admin_login(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size) if size > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self.send_json(400, api_error_payload("请求体不是合法 JSON"))
            return
        key = str(payload.get("api_key") or payload.get("token") or "").strip()
        accepted = self.server.accepted_api_keys()
        if accepted and key not in accepted:
            self.send_json(401, api_error_payload("API Key 无效（需主密钥或已登记的客户端密钥）", "authentication_error"))
            return
        # When no keys configured, accept empty login for loopback admin.
        cookie_value = key if key else (self.server.api_key or "")
        body = json.dumps({"ok": True, "key": cookie_value}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie_value:
            # SameSite=Lax so top-level navigations keep the cookie; workbench primarily uses Bearer from localStorage.
            self.send_header(
                "Set-Cookie",
                f"grokbot2api_key={cookie_value}; Path=/; SameSite=Lax",
            )
        self.send_header("Connection", "close")
        self.add_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise ClientDisconnected from exc

    def handle_admin_models_update(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("invalid body")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            action = str(payload.get("action") or "").strip().lower()
            if action in {"add", "create", "add_custom"} or payload.get("alias"):
                if action in {"", "update"} and "enabled" in payload and "upstream_id" not in payload:
                    pass
                elif "upstream_id" in payload or action in {"add", "create", "add_custom"}:
                    spec = self.server.catalogue.add_custom_model(payload)
                    status = admin_status_payload(self.server)
                    status["added"] = spec.to_public()
                    self.send_json(200, status)
                    return
            if "default_alias" in payload:
                self.server.catalogue.set_default(str(payload["default_alias"]))
            toggles = payload.get("enabled")
            if isinstance(toggles, dict):
                for alias, enabled in toggles.items():
                    self.server.catalogue.set_enabled(str(alias), bool(enabled))
            self.send_json(200, admin_status_payload(self.server))
        except KeyError as exc:
            self.send_json(404, api_error_payload(f"unknown model {exc}"))
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            self.send_json(400, api_error_payload(str(exc)))

    def handle_admin_keys(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("请求体无效")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            action = str(payload.get("action") or "").strip().lower()
            if action in {"create", "add"}:
                entry = self.server.catalogue.add_client_key(
                    str(payload.get("key") or ""),
                    name=str(payload.get("name") or ""),
                )
                self.send_json(200, {"ok": True, "key": entry, "status": admin_status_payload(self.server)})
                return
            if action in {"revoke", "delete", "remove"}:
                key_id = str(payload.get("id") or payload.get("key_id") or "")
                if not key_id:
                    raise ValueError("吊销密钥需要提供 id")
                self.server.catalogue.revoke_client_key(key_id)
                self.send_json(200, admin_status_payload(self.server))
                return
            raise ValueError("action 必须是 create 或 revoke")
        except KeyError as exc:
            self.send_json(404, api_error_payload(f"未知密钥 {exc}"))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, api_error_payload(str(exc)))

    def handle_image_generations(self, request: dict[str, Any]) -> None:
        prompt = request.get("prompt") or request.get("description") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        model = str(request.get("model") or "cursor-generate-image").strip()
        n = request.get("n", 1)
        if n not in (1, None):
            try:
                if int(n) != 1:
                    raise ValueError("only n=1 is supported")
            except (TypeError, ValueError) as exc:
                raise ValueError("only n=1 is supported") from exc
        response_format = str(request.get("response_format") or "url")
        aspect_ratio = request.get("aspect_ratio") or request.get("aspectRatio")
        size = request.get("size")
        if aspect_ratio is not None and not isinstance(aspect_ratio, str):
            raise ValueError("aspect_ratio must be a string")
        if size is not None and not isinstance(size, str):
            raise ValueError("size must be a string")

        refs: list[dict[str, str]] = []
        raw_refs = request.get("reference_images") or request.get("image") or []
        if isinstance(raw_refs, str):
            raw_refs = [raw_refs]
        if isinstance(raw_refs, list):
            for item in raw_refs:
                if isinstance(item, str) and item:
                    if item.startswith("data:"):
                        # data URL
                        try:
                            header, payload = item.split(",", 1)
                            mime = "image/png"
                            if ";" in header:
                                mime = header[5:].split(";", 1)[0] or mime
                            refs.append({"data": payload, "mime_type": mime})
                        except ValueError:
                            refs.append({"data": item, "mime_type": "image/png"})
                    else:
                        refs.append({"data": item, "mime_type": "image/png"})
                elif isinstance(item, dict):
                    data = str(item.get("data") or item.get("b64_json") or "")
                    mime = str(item.get("mime_type") or item.get("mimeType") or "image/png")
                    if data:
                        refs.append({"data": data, "mime_type": mime})

        generate = getattr(self.server.backend, "generate_image", None)
        if not callable(generate):
            raise RuntimeError("backend does not support image generation")
        public_base = f"http://{self.headers.get('Host')}" if self.headers.get("Host") else ""
        payload = generate(
            prompt.strip(),
            model=model,
            aspect_ratio=aspect_ratio,
            size=size,
            response_format=response_format,
            reference_images=refs or None,
            media_store=getattr(self.server, "media_store", None),
            public_base=public_base,
        )
        self.send_json(200, payload)

    def handle_completion(self, request: dict[str, Any]) -> None:
        model = str(request.get("model") or self.server.backend.options.model)
        tools = request.get("tools") if isinstance(request.get("tools"), list) else []
        stream = bool(request.get("stream"))
        tool_results = sum(
            1 for message in request["messages"] if isinstance(message, dict) and message.get("role") == "tool"
        )
        self.log_message(
            "completion model=%s messages=%d tools=%d tool_results=%d stream=%s",
            model,
            len(request["messages"]),
            len(tools),
            tool_results,
            stream,
        )

        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        if stream:
            self.handle_streaming_completion(completion_id, created, model, request["messages"], tools, request)
            return

        result, content, calls = self.server.backend.complete(model, request["messages"], tools, request)
        if not result.get("ok"):
            raise RuntimeError(coerce_error_message(result.get("error")) or f"upstream HTTP {result.get('httpStatus')}")

        finish_reason = "tool_calls" if calls else "stop"
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self._last_usage = usage
        self._last_retried = bool(result.get("retried"))
        self._last_retry_reason = str(result.get("retry_reason") or "")
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        extended = result.get("extended_usage") if isinstance(result.get("extended_usage"), dict) else {}
        self.log_message(
            "native response finish=%s tool_calls=%d prompt_tokens=%d completion_tokens=%d cached_tokens=%d context_window=%d",
            finish_reason,
            len(calls),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            details.get("cached_tokens", 0),
            extended.get("context_window", 0),
        )

        message: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
        self.send_json(
            200,
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": result.get("usage") or {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            },
        )

    def stream_event(
        self,
        completion_id: str,
        created: int,
        model: str,
        delta: dict[str, Any],
        finish_reason: str | None = None,
    ) -> None:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        try:
            self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected from exc

    def stream_heartbeat(self) -> None:
        try:
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected from exc

    def stream_done(self) -> None:
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected from exc

    def handle_streaming_completion(
        self,
        completion_id: str,
        created: int,
        model: str,
        messages: list[Any],
        tools: list[Any],
        request: dict[str, Any],
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.add_cors_headers()
        self.end_headers()
        self.stream_event(completion_id, created, model, {"role": "assistant"})

        completed: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke_upstream() -> None:
            try:
                completed.put((True, self.server.backend.complete(model, messages, tools, request)))
            except BaseException as exc:
                completed.put((False, exc))

        worker = threading.Thread(target=invoke_upstream, daemon=True, name=f"upstream-{completion_id[-8:]}")
        worker.start()
        while True:
            try:
                succeeded, outcome = completed.get(timeout=STREAM_HEARTBEAT_SECONDS)
                break
            except queue.Empty:
                self.stream_heartbeat()

        if not succeeded:
            self.log_message("upstream exception during stream: %s", outcome)
            error = api_error_payload(outcome, "upstream_error")
            try:
                self.wfile.write(b"data: " + json.dumps(error, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.stream_done()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                raise ClientDisconnected from exc
            return

        result, content, calls = outcome
        if not result.get("ok"):
            message = coerce_error_message(result.get("error")) or f"upstream HTTP {result.get('httpStatus')}"
            self.log_message("upstream error during stream: %s", message)
            error = api_error_payload(message, "upstream_error")
            try:
                self.wfile.write(b"data: " + json.dumps(error, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.stream_done()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                raise ClientDisconnected from exc
            return

        finish_reason = "tool_calls" if calls else "stop"
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self._last_usage = usage
        self._last_retried = bool(result.get("retried"))
        self._last_retry_reason = str(result.get("retry_reason") or "")
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        extended = result.get("extended_usage") if isinstance(result.get("extended_usage"), dict) else {}
        self.log_message(
            "native response finish=%s tool_calls=%d prompt_tokens=%d completion_tokens=%d cached_tokens=%d context_window=%d",
            finish_reason,
            len(calls),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            details.get("cached_tokens", 0),
            extended.get("context_window", 0),
        )

        if calls:
            deltas = []
            for index, call in enumerate(calls):
                deltas.append(
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": call["function"],
                    }
                )
            self.stream_event(completion_id, created, model, {"tool_calls": deltas})
        elif content:
            self.stream_event(completion_id, created, model, {"content": content})
        self.stream_event(completion_id, created, model, {}, finish_reason)
        self.stream_done()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1", help="listen address; keep loopback unless API auth is enabled")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--model",
        default="grok-4.7-high",
        help="upstream model used when the client omits model (fallback)",
    )
    parser.add_argument(
        "--default-alias",
        default=DEFAULT_ALIAS,
        help="default client-facing alias listed as catalogue default",
    )
    parser.add_argument(
        "--admin-config",
        type=Path,
        default=Path(__file__).with_name("admin_config.json"),
        help="JSON file for persisted admin toggles / default alias",
    )
    parser.add_argument("--upstream-script", type=Path, default=DEFAULT_UPSTREAM_SCRIPT)
    parser.add_argument("--backend-url", default="")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--max-mode", action="store_true")
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--client-type", default="sand")
    parser.add_argument("--client-version", default="0.58.0")
    parser.add_argument(
        "--chat-mode",
        default=os.environ.get("GROKBOT_CHAT_MODE", "agent"),
        choices=("agent", "stream"),
        help="agent=GrokBotService (official UI path, default); stream=InferenceService/Stream fallback",
    )
    parser.add_argument(
        "--grokbot-agent-id",
        default=os.environ.get("GROKBOT_AGENT_ID", ""),
        help="pin GrokBotService agent UUID (default: find/create name grokbot2api)",
    )
    parser.add_argument(
        "--grokbot-agent-name",
        default=os.environ.get("GROKBOT_AGENT_NAME", "grokbot2api"),
        help="GrokBotService agent display name used when --grokbot-agent-id is empty",
    )
    parser.add_argument("--client-source", default=os.environ.get("SAND_CLIENT_SOURCE") or "sand-desktop")
    parser.add_argument("--client-os", default=os.environ.get("SAND_CLIENT_OS") or "")
    parser.add_argument("--namespace", default="prod")
    parser.add_argument("--team-id", default="")
    parser.add_argument("--timeout-ms", type=int, default=600000)
    parser.add_argument(
        "--min-max-tokens",
        type=int,
        default=DEFAULT_MIN_MAX_TOKENS,
        help=(
            "smallest output ceiling to send upstream; lower client values are raised to it "
            "(0 disables the floor)"
        ),
    )
    parser.add_argument(
        "--api-key-env",
        default="GROK_BUILD_PROXY_API_KEY",
        help="optional env var containing the local proxy Bearer token",
    )
    parser.add_argument(
        "--cors-origins",
        default="",
        help="comma-separated allowed CORS origins (empty disables CORS; * allows any)",
    )
    return parser.parse_args()


def main() -> None:
    options = parse_args()
    if options.listen not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(options.api_key_env, ""):
        raise SystemExit("refusing non-loopback listen address without a proxy API key")
    catalogue = ModelCatalogue(
        default_alias=options.default_alias,
        config_path=options.admin_config,
        fallback_upstream=options.model,
    )
    backend = SandBackend(options, catalogue=catalogue)
    api_key = os.environ.get(options.api_key_env, "")
    cors_origins = [o.strip() for o in str(options.cors_origins or "").split(",") if o.strip()]
    server = ProxyServer(
        (options.listen, options.port),
        backend,
        api_key,
        cors_origins=cors_origins,
    )
    print(f"grokbot2api listening on http://{options.listen}:{options.port}/v1", flush=True)
    print(f"admin: http://{options.listen}:{options.port}/admin", flush=True)
    print(f"docs: http://{options.listen}:{options.port}/docs", flush=True)
    print(
        f"default alias: {catalogue.default_alias}; fallback upstream: {options.model}; "
        f"models: {len(catalogue.list_public())}; upstream: {options.upstream_script}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

