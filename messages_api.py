"""Anthropic Messages API adapter (POST /v1/messages).

Converts Anthropic request/response shapes to the proxy's native OpenAI-style
chat path so Claude Code / Anthropic SDK basic chat+tools can talk to the
Cursor sand InferenceService backend.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any

from api_common import ClientDisconnected, normalize_tool_call_id


def _system_to_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(system)


def _image_source_to_openai(source: dict[str, Any]) -> dict[str, Any] | None:
    """Map Anthropic image source → OpenAI image_url part."""
    if not isinstance(source, dict):
        return None
    source_type = str(source.get("type") or "")
    if source_type == "base64":
        media_type = str(source.get("media_type") or "image/png")
        data = str(source.get("data") or "")
        if not data:
            return None
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data}"},
        }
    if source_type == "url":
        url = str(source.get("url") or "")
        if not url:
            return None
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _content_blocks_to_openai(content: Any) -> Any:
    """Flatten Anthropic content (string or blocks) to OpenAI message content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    text_parts: list[str] = []
    openai_parts: list[dict[str, Any]] = []
    has_image = False

    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            openai_parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype == "text":
            text = str(block.get("text") or "")
            text_parts.append(text)
            openai_parts.append({"type": "text", "text": text})
        elif btype == "image":
            image_part = _image_source_to_openai(block.get("source") or {})
            if image_part:
                has_image = True
                openai_parts.append(image_part)
        # tool_use / tool_result handled at message level

    if has_image:
        return openai_parts
    return "\n".join(text_parts)


def anthropic_tools_to_openai(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": schema,
                },
            }
        )
    return converted


def anthropic_messages_to_openai(
    messages: Any,
    *,
    system: Any = None,
) -> list[dict[str, Any]]:
    """Convert Anthropic messages (+ optional system) to OpenAI chat messages."""
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")

    out: list[dict[str, Any]] = []
    system_text = _system_to_text(system)
    if system_text:
        out.append({"role": "system", "content": system_text})

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")

        if role == "user":
            if isinstance(content, list):
                # May mix text/image with tool_result blocks.
                tool_results = [
                    b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                other = [
                    b
                    for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result")
                ]
                for tr in tool_results:
                    tool_content = tr.get("content")
                    if isinstance(tool_content, list):
                        tool_text = "\n".join(
                            str(b.get("text") or "")
                            if isinstance(b, dict) and b.get("type") == "text"
                            else (b if isinstance(b, str) else json.dumps(b, ensure_ascii=False))
                            for b in tool_content
                        )
                    elif tool_content is None:
                        tool_text = ""
                    else:
                        tool_text = (
                            tool_content
                            if isinstance(tool_content, str)
                            else json.dumps(tool_content, ensure_ascii=False)
                        )
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": normalize_tool_call_id(tr.get("tool_use_id")),
                            "content": tool_text,
                        }
                    )
                if other:
                    out.append({"role": "user", "content": _content_blocks_to_openai(other)})
            else:
                out.append({"role": "user", "content": _content_blocks_to_openai(content)})
            continue

        if role == "assistant":
            text = ""
            tool_calls: list[dict[str, Any]] = []
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "")
                    if btype == "text":
                        texts.append(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        call_id = normalize_tool_call_id(block.get("id"))
                        name = str(block.get("name") or "")
                        raw_input = block.get("input")
                        if isinstance(raw_input, str):
                            args = raw_input
                        else:
                            args = json.dumps(
                                raw_input if isinstance(raw_input, dict) else {},
                                ensure_ascii=False,
                            )
                        tool_calls.append(
                            {
                                "id": call_id or f"call_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {"name": name, "arguments": args or "{}"},
                            }
                        )
                text = "\n".join(texts)
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": text if text else (None if tool_calls else ""),
            }
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

    return out


def openai_result_to_anthropic(
    *,
    model: str,
    content: str | None,
    tool_calls: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build a non-streaming Anthropic Messages response."""
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for call in tool_calls:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        args_raw = function.get("arguments") or "{}"
        try:
            parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            parsed = {"_raw": args_raw}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        blocks.append(
            {
                "type": "tool_use",
                "id": normalize_tool_call_id(call.get("id")) or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": str(function.get("name") or ""),
                "input": parsed,
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop_reason = "tool_use" if tool_calls else "end_turn"
    usage = usage if isinstance(usage, dict) else {}
    return {
        "id": message_id or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def anthropic_request_to_chat(request: dict[str, Any]) -> dict[str, Any]:
    """Map an Anthropic Messages body onto the chat-completions request shape."""
    model = str(request.get("model") or "")
    messages = anthropic_messages_to_openai(
        request.get("messages"),
        system=request.get("system"),
    )
    tools = anthropic_tools_to_openai(request.get("tools"))
    chat: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(request.get("stream")),
    }
    if tools:
        chat["tools"] = tools
    if "max_tokens" in request:
        chat["max_tokens"] = request["max_tokens"]
    if "temperature" in request:
        chat["temperature"] = request["temperature"]
    if "top_p" in request:
        chat["top_p"] = request["top_p"]
    if "stop_sequences" in request and isinstance(request["stop_sequences"], list):
        chat["stop"] = request["stop_sequences"]
    return chat


class MessagesApiMixin:
    """HTTP handlers mixed into ProxyHandler for Anthropic Messages."""

    def handle_messages(self, request: dict[str, Any]) -> None:
        if not isinstance(request.get("messages"), list):
            raise ValueError("messages must be an array")
        if "max_tokens" not in request:
            # Anthropic requires max_tokens; default for convenience.
            request = dict(request)
            request["max_tokens"] = 4096

        chat = anthropic_request_to_chat(request)
        model = str(chat.get("model") or self.server.backend.options.model)
        tools = chat.get("tools") if isinstance(chat.get("tools"), list) else []
        stream = bool(chat.get("stream"))
        messages = chat["messages"]

        self.log_message(
            "messages model=%s messages=%d tools=%d stream=%s",
            model,
            len(messages),
            len(tools),
            stream,
        )

        message_id = f"msg_{uuid.uuid4().hex}"
        if stream:
            self.handle_streaming_messages(message_id, model, messages, tools, chat)
            return

        result, content, calls = self.server.backend.complete(model, messages, tools, chat)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or f"upstream HTTP {result.get('httpStatus')}"))

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        payload = openai_result_to_anthropic(
            model=model,
            content=content,
            tool_calls=calls,
            usage=usage,
            message_id=message_id,
        )
        # Stash usage on the handler for audit logging by the router.
        self._last_usage = usage
        self._last_retried = bool(result.get("retried"))
        self._last_retry_reason = str(result.get("retry_reason") or "")
        self.send_json(200, payload)

    def _write_sse_event(self, event: str, data: dict[str, Any]) -> None:
        try:
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(
                b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n"
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected from exc

    def handle_streaming_messages(
        self,
        message_id: str,
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

        completed: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke_upstream() -> None:
            try:
                completed.put((True, self.server.backend.complete(model, messages, tools, request)))
            except BaseException as exc:  # noqa: BLE001 — surface to SSE
                completed.put((False, exc))

        worker = threading.Thread(
            target=invoke_upstream, daemon=True, name=f"anthropic-{message_id[-8:]}"
        )
        worker.start()

        heartbeat = getattr(self, "stream_heartbeat_seconds", 1.0)
        while True:
            try:
                succeeded, outcome = completed.get(timeout=heartbeat)
                break
            except queue.Empty:
                try:
                    self.wfile.write(b"event: ping\ndata: {}\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise ClientDisconnected from exc

        if not succeeded:
            self._write_sse_event(
                "error",
                {"type": "error", "error": {"type": "api_error", "message": str(outcome)}},
            )
            return

        result, content, calls = outcome
        if not result.get("ok"):
            message = str(result.get("error") or f"upstream HTTP {result.get('httpStatus')}")
            self._write_sse_event(
                "error",
                {"type": "error", "error": {"type": "api_error", "message": message}},
            )
            return

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self._last_usage = usage
        self._last_retried = bool(result.get("retried"))
        self._last_retry_reason = str(result.get("retry_reason") or "")

        payload = openai_result_to_anthropic(
            model=model,
            content=content,
            tool_calls=calls,
            usage=usage,
            message_id=message_id,
        )

        self._write_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": payload["usage"]["input_tokens"], "output_tokens": 0},
                },
            },
        )

        for index, block in enumerate(payload["content"]):
            self._write_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": {**block, **(
                    {"input": {}} if block.get("type") == "tool_use" else {}
                )}},
            )
            # For tool_use, re-emit start with empty input then delta the JSON.
            if block.get("type") == "text":
                self._write_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block.get("text") or ""},
                    },
                )
            elif block.get("type") == "tool_use":
                # Fix start block: Anthropic wants input:{} on start, then input_json_delta.
                # We already sent full block; emit input_json_delta for SDK compatibility.
                self._write_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    },
                )
            self._write_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )

        self._write_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": payload["usage"]["output_tokens"]},
            },
        )
        self._write_sse_event("message_stop", {"type": "message_stop"})
