"""GrokBotService chat path — mirrors official Grok Bot desktop.

Official UI does NOT call InferenceService/Stream for chat. It uses:
  POST /aiserver.v1.GrokBotService/SendGrokBotUserMessage
then reads the assistant reply from:
  POST /aiserver.v1.GrokBotService/ListGrokBotTranscriptEntries
(and optionally GetGrokBotSendStatus / WatchGrokBotTranscripts).

Auth: session JWT (type=session) from /sand-box/inference-credential's accessToken.
      grok_bot JWT returns ERROR_NOT_LOGGED_IN on GrokBotService (observed).
Transport: Connect unary JSON (application/json) — matches working probes on 0.58.0.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import uuid
import http.client
import urllib.parse
from typing import Any, Callable

GROKBOT_SERVICE = "aiserver.v1.GrokBotService"
DEFAULT_AGENT_NAME = "grokbot2api"
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_POLL_TIMEOUT_S = 180.0


class GrokBotChatError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 0, payload: Any = None):
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload


def _extract_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return str(content)


def messages_to_prompt(messages: list[Any]) -> str:
    """Flatten OpenAI-style messages into one user prompt for the agent turn."""
    blocks: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        text = _extract_text_content(message.get("content")).strip()
        if not text:
            continue
        if role == "system":
            blocks.append(f"[system]\n{text}")
        elif role == "assistant":
            blocks.append(f"[assistant]\n{text}")
        elif role == "tool":
            blocks.append(f"[tool]\n{text}")
        else:
            blocks.append(text)
    return "\n\n".join(blocks).strip()


def decode_entry_body(body: Any) -> dict[str, Any] | None:
    """Transcript entry body is base64(JSON) over Connect JSON."""
    if body is None:
        return None
    raw: bytes
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    elif isinstance(body, str):
        s = body.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        try:
            raw = base64.b64decode(s)
        except Exception:
            return None
    else:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def assistant_text_from_entry(entry: dict[str, Any]) -> str | None:
    kind = str(entry.get("entryKind") or entry.get("entry_kind") or "")
    parsed = decode_entry_body(entry.get("body"))
    if not parsed:
        return None
    pkind = str(parsed.get("kind") or kind)
    if pkind == "send-message":
        message = parsed.get("message")
        if isinstance(message, dict) and message.get("type") == "text":
            content = message.get("content")
            return content if isinstance(content, str) else None
    if pkind == "message" and str(parsed.get("role") or "") == "assistant":
        content = parsed.get("content")
        return content if isinstance(content, str) else None
    return None


def is_our_user_entry(entry: dict[str, Any], message_id: str) -> bool:
    parsed = decode_entry_body(entry.get("body"))
    if not parsed:
        return False
    if str(parsed.get("kind") or "") != "message":
        return False
    if str(parsed.get("role") or "") != "user":
        return False
    nonce = parsed.get("clientNonce") or parsed.get("client_nonce") or ""
    return str(nonce) == message_id or str(entry.get("entryId") or "") == message_id


class GrokBotServiceClient:
    def __init__(
        self,
        *,
        session_token: str,
        backend_url: str,
        meta: dict[str, str],
        machine_id: str,
        timeout_ms: int = 120000,
        team_id: str | None = None,
    ):
        self.session_token = session_token
        self.backend_url = backend_url.rstrip("/")
        self.meta = dict(meta)
        self.machine_id = machine_id
        self.timeout_ms = int(timeout_ms)
        self.team_id = team_id
        parsed = urllib.parse.urlparse(self.backend_url)
        self._host = parsed.hostname or "api2.cursor.sh"
        self._port = parsed.port or (443 if (parsed.scheme or "https") == "https" else 80)
        self._https = (parsed.scheme or "https") == "https"

    def _headers(self) -> dict[str, str]:
        # Local import avoids circular deps when sand_inference loads us.
        from sand_inference import cursor_checksum  # type: ignore

        headers = {
            "authorization": f"Bearer {self.session_token}",
            "content-type": "application/json",
            "connect-protocol-version": "1",
            "connect-timeout-ms": str(self.timeout_ms),
            "x-cursor-checksum": cursor_checksum(self.machine_id),
            "x-ghost-mode": "true",
            "x-request-id": str(uuid.uuid4()),
            **self.meta,
        }
        if self.team_id:
            headers["x-cursor-team-id"] = str(self.team_id)
        return headers

    def rpc(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path = f"/{GROKBOT_SERVICE}/{method}"
        body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        if self._https:
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout_ms / 1000, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout_ms / 1000)
        try:
            conn.request("POST", path, body=body, headers=self._headers())
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()
        text = raw.decode("utf-8", "replace") if raw else ""
        try:
            parsed: Any = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"_raw": text[:500]}
        if status >= 400:
            message = ""
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or parsed.get("code") or "")
            raise GrokBotChatError(
                message or f"GrokBotService/{method} HTTP {status}",
                http_status=status,
                payload=parsed,
            )
        if not isinstance(parsed, dict):
            raise GrokBotChatError(
                f"GrokBotService/{method} returned non-object",
                http_status=status,
                payload=parsed,
            )
        return parsed

    def list_agents(self) -> list[dict[str, Any]]:
        resp = self.rpc("ListGrokBotAgents", {})
        agents = resp.get("agents")
        return list(agents) if isinstance(agents, list) else []

    def create_agent(self, *, name: str, agent_id: str | None = None) -> dict[str, Any]:
        agent_uuid = agent_id or str(uuid.uuid4())
        resp = self.rpc(
            "CreateGrokBotAgent",
            {
                "legacyAgentId": agent_uuid,
                "agentId": agent_uuid,
                "name": name,
                "description": "OpenAI-compatible API bridge agent (grokbot2api)",
                "title": name,
                "avatarShape": "circle",
                "avatarColor": "#6E56CF",
                "harness": "TEMPORAL",
                "kickstartRequested": False,
                "introductionSuppressed": True,
                "createIntent": "FRESH",
                "purpose": "api",
                "origin": "grokbot2api",
            },
        )
        agent = resp.get("agent")
        if isinstance(agent, dict):
            return agent
        raise GrokBotChatError("CreateGrokBotAgent returned no agent", payload=resp)

    def resolve_agent(
        self,
        *,
        agent_id: str | None = None,
        agent_name: str = DEFAULT_AGENT_NAME,
        create_if_missing: bool = True,
    ) -> dict[str, Any]:
        agents = self.list_agents()
        if agent_id:
            for agent in agents:
                if str(agent.get("agentId") or "") == agent_id or str(agent.get("id") or "") == agent_id:
                    return agent
            raise GrokBotChatError(f"agent not found: {agent_id}")
        for agent in agents:
            if str(agent.get("name") or "") == agent_name:
                return agent
        if create_if_missing:
            return self.create_agent(name=agent_name)
        if agents:
            return agents[0]
        raise GrokBotChatError("no Grok Bot agents available; create one in the desktop app or allow auto-create")

    def send_user_message(
        self,
        *,
        agent_id: str,
        text: str,
        message_id: str | None = None,
        session_id: str = "",
        source: str = "DESKTOP",
    ) -> tuple[str, dict[str, Any]]:
        mid = message_id or str(uuid.uuid4())
        resp = self.rpc(
            "SendGrokBotUserMessage",
            {
                "agentId": agent_id,
                "messageId": mid,
                "text": text,
                "sentAtMs": str(int(time.time() * 1000)),
                "isFork": False,
                "source": source,
                "sessionId": session_id or "",
                "machineId": self.machine_id,
            },
        )
        delivery = str(resp.get("delivery") or "")
        if "REFUSED" in delivery:
            refusal = resp.get("refusal") if isinstance(resp.get("refusal"), dict) else {}
            raise GrokBotChatError(
                str(refusal.get("message") or refusal.get("failureCode") or "SendGrokBotUserMessage refused"),
                payload=resp,
            )
        if "ACCEPTED" not in delivery and "DUPLICATE" not in delivery and not resp.get("dispatched"):
            raise GrokBotChatError(
                f"unexpected delivery={delivery or resp}",
                payload=resp,
            )
        return mid, resp

    def get_send_status(self, *, agent_id: str, message_id: str, session_id: str = "") -> dict[str, Any]:
        return self.rpc(
            "GetGrokBotSendStatus",
            {"agentId": agent_id, "messageId": message_id, "sessionId": session_id or ""},
        )

    def list_transcript_entries(
        self, *, agent_id: str, limit: int = 30, session_id: str = ""
    ) -> dict[str, Any]:
        return self.rpc(
            "ListGrokBotTranscriptEntries",
            {"agentId": agent_id, "limit": int(limit), "sessionId": session_id or ""},
        )


def wait_for_assistant_reply(
    client: GrokBotServiceClient,
    *,
    agent_id: str,
    message_id: str,
    session_id: str = "",
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Poll send-status then transcript until an assistant send-message appears after our turn.

    ListGrokBotTranscriptEntries returns newest-first. Our user row is identified by
    GetGrokBotSendStatus.echoEntryId or body.clientNonce == message_id. Assistant
    ``send-message`` rows that are newer (lower index) than the user row are the reply.
    """
    deadline = time.time() + poll_timeout_s
    echo_entry_id: str | None = None

    while time.time() < deadline:
        status = client.get_send_status(agent_id=agent_id, message_id=message_id, session_id=session_id)
        code = str(status.get("status") or "")
        if on_status:
            on_status(code)
        if "REJECTED" in code:
            raise GrokBotChatError(
                str(status.get("rejectionCode") or "message rejected"),
                payload=status,
            )
        if "ACCEPTED" in code:
            echo = status.get("echoEntryId") or status.get("echo_entry_id")
            if isinstance(echo, str) and echo:
                echo_entry_id = echo
            break
        time.sleep(poll_interval_s)

    last_n = 0
    while time.time() < deadline:
        tr = client.list_transcript_entries(agent_id=agent_id, limit=40, session_id=session_id)
        entries = tr.get("entries") if isinstance(tr.get("entries"), list) else []
        last_n = len(entries)
        user_idx = None
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entryId") or "")
            if echo_entry_id and entry_id == echo_entry_id:
                user_idx = index
                break
            if is_our_user_entry(entry, message_id):
                user_idx = index
                break
        if user_idx is None:
            time.sleep(poll_interval_s)
            continue
        for entry in entries[:user_idx]:
            if not isinstance(entry, dict):
                continue
            text = assistant_text_from_entry(entry)
            if text is not None:
                return {
                    "text": text,
                    "entryId": str(entry.get("entryId") or ""),
                    "entryKind": entry.get("entryKind"),
                    "echoEntryId": echo_entry_id,
                    "generation": tr.get("generation"),
                }
        time.sleep(poll_interval_s)

    raise GrokBotChatError(
        f"timed out waiting for assistant reply after {poll_timeout_s:.0f}s (entries={last_n})",
        http_status=504,
    )


def chat_via_grokbot_service(
    *,
    session_token: str,
    backend_url: str,
    meta: dict[str, str],
    machine_id: str,
    messages: list[Any],
    agent_id: str | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
    timeout_ms: int = 180000,
    team_id: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    create_agent_if_missing: bool = True,
) -> dict[str, Any]:
    """Send one chat turn through GrokBotService and return a Stream-compatible result dict."""
    prompt = messages_to_prompt(messages)
    if not prompt:
        raise GrokBotChatError("no user text to send")

    client = GrokBotServiceClient(
        session_token=session_token,
        backend_url=backend_url,
        meta=meta,
        machine_id=machine_id,
        timeout_ms=timeout_ms,
        team_id=team_id,
    )
    agent = client.resolve_agent(
        agent_id=agent_id,
        agent_name=agent_name,
        create_if_missing=create_agent_if_missing,
    )
    resolved_agent_id = str(agent.get("agentId") or agent.get("id") or "")
    session_id = str(agent.get("viewerSessionId") or "")
    if not resolved_agent_id:
        raise GrokBotChatError("resolved agent has no agentId", payload=agent)

    message_id, send_resp = client.send_user_message(
        agent_id=resolved_agent_id,
        text=prompt,
        session_id=session_id,
    )
    poll_timeout_s = max(30.0, (timeout_ms / 1000.0) - 5.0)
    reply = wait_for_assistant_reply(
        client,
        agent_id=resolved_agent_id,
        message_id=message_id,
        session_id=session_id,
        poll_interval_s=poll_interval_s,
        poll_timeout_s=poll_timeout_s,
    )
    return {
        "ok": True,
        "httpStatus": 200,
        "text": reply.get("text") or "",
        "tool_calls": [],
        "requestId": message_id,
        "model": "grokbot-service",
        "usage": {},
        "error": None,
        "transport": "GrokBotService/SendGrokBotUserMessage",
        "agentId": resolved_agent_id,
        "agentName": agent.get("name"),
        "send": send_resp,
        "replyMeta": {
            "entryId": reply.get("entryId"),
            "echoEntryId": reply.get("echoEntryId"),
            "generation": reply.get("generation"),
        },
    }