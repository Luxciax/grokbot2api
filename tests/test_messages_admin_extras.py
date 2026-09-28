"""Anthropic Messages, multi client keys, audits, CORS, docs."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grokbot2api as bridge  # noqa: E402
import messages_api  # noqa: E402
import model_catalogue  # noqa: E402


class FakeBackend:
    options = SimpleNamespace(model="grok-4.6")

    def complete(self, model, messages, tools, request):
        usage = {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        tool_roles = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if not tool_roles and tools:
            calls = [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                }
            ]
            return {"ok": True, "usage": usage, "retried": False}, None, calls
        return {"ok": True, "usage": usage, "retried": False}, "hello from sand", []


class MessagesConversionTests(unittest.TestCase):
    def test_system_and_tools_round_trip(self):
        chat = messages_api.anthropic_request_to_chat(
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 128,
                "system": "Be brief.",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ],
            }
        )
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertEqual(chat["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(chat["max_tokens"], 128)

    def test_tool_result_blocks(self):
        msgs = messages_api.anthropic_messages_to_openai(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "72F",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(msgs[0]["role"], "tool")
        self.assertEqual(msgs[0]["content"], "72F")

    def test_image_block_to_openai(self):
        content = messages_api._content_blocks_to_openai(
            [
                {"type": "text", "text": "see"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "AAAA",
                    },
                },
            ]
        )
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))


class MessagesHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_heartbeat = bridge.STREAM_HEARTBEAT_SECONDS
        bridge.STREAM_HEARTBEAT_SECONDS = 0.01
        cls.server = bridge.ProxyServer(("127.0.0.1", 0), FakeBackend(), "")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        bridge.STREAM_HEARTBEAT_SECONDS = cls.previous_heartbeat

    def post_json(self, path, payload, headers=None):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.read().decode(), dict(response.headers)

    def test_messages_non_stream_text(self):
        status, body, _ = self.post_json(
            "/v1/messages",
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"][0]["type"], "text")
        self.assertEqual(payload["content"][0]["text"], "hello from sand")
        self.assertEqual(payload["stop_reason"], "end_turn")

    def test_messages_tool_use(self):
        status, body, _ = self.post_json(
            "/v1/messages",
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        )
        payload = json.loads(body)
        self.assertEqual(payload["stop_reason"], "tool_use")
        self.assertEqual(payload["content"][0]["type"], "tool_use")
        self.assertEqual(payload["content"][0]["name"], "get_weather")

    def test_messages_stream_sse(self):
        status, body, _ = self.post_json(
            "/v1/messages",
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("event: message_start", body)
        self.assertIn("event: content_block_delta", body)
        self.assertIn("event: message_stop", body)

    def test_messages_alias_path(self):
        status, body, _ = self.post_json(
            "/messages",
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["type"], "message")

    def test_docs_and_rich_health(self):
        with urllib.request.urlopen(self.base_url + "/docs", timeout=3) as response:
            html = response.read().decode()
        self.assertIn("/v1/messages", html)
        with urllib.request.urlopen(self.base_url + "/health", timeout=3) as response:
            health = json.load(response)
        self.assertTrue(health["ok"])
        self.assertIn("version", health)

    def test_audit_after_request(self):
        self.post_json(
            "/v1/messages",
            {
                "model": "cursor-grok-4-6",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "audit me"}],
            },
        )
        with urllib.request.urlopen(self.base_url + "/admin/api/audits", timeout=3) as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertEqual(payload["audits"][-1]["path"], "/v1/messages")


class ClientKeyAndCorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        config_path = Path(cls.tmp.name) / "admin_config.json"
        catalogue = model_catalogue.ModelCatalogue(config_path=config_path)
        catalogue.add_client_key("client-key-abcdef", name="ci")
        backend = FakeBackend()
        backend.catalogue = catalogue  # type: ignore[attr-defined]
        cls.server = bridge.ProxyServer(
            ("127.0.0.1", 0),
            backend,
            "primary-secret-key",
            cors_origins=["http://localhost:3000"],
        )
        cls.server.catalogue = catalogue
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base_url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), dict(error.headers)

    def test_client_key_accepted_via_x_api_key(self):
        status, body, _ = self.get("/v1/models", {"x-api-key": "client-key-abcdef"})
        self.assertEqual(status, 200)
        self.assertIn("cursor-grok-4-7", body)
        self.assertIn("cursor-grok-4-6", body)

    def test_primary_key_still_works(self):
        status, _, _ = self.get("/v1/models", {"Authorization": "Bearer primary-secret-key"})
        self.assertEqual(status, 200)

    def test_wrong_key_rejected(self):
        status, _, _ = self.get("/v1/models", {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_admin_create_and_revoke_key(self):
        payload = json.dumps(
            {"action": "create", "key": "another-client-key9", "name": "tmp"}
        ).encode()
        req = urllib.request.Request(
            self.base_url + "/admin/api/keys",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer primary-secret-key",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            created = json.load(response)
        self.assertTrue(created["ok"])
        key_id = created["key"]["id"]
        status, _, _ = self.get("/v1/models", {"x-api-key": "another-client-key9"})
        self.assertEqual(status, 200)

        revoke = json.dumps({"action": "revoke", "id": key_id}).encode()
        req = urllib.request.Request(
            self.base_url + "/admin/api/keys",
            data=revoke,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer primary-secret-key",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 200)
        status, _, _ = self.get("/v1/models", {"x-api-key": "another-client-key9"})
        self.assertEqual(status, 401)

    def test_cors_preflight_and_response(self):
        req = urllib.request.Request(
            self.base_url + "/v1/models",
            method="OPTIONS",
            headers={"Origin": "http://localhost:3000"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "http://localhost:3000")

        status, _, headers = self.get(
            "/health",
            {"Origin": "http://localhost:3000"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://localhost:3000")


class CatalogueClientKeyPersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_config.json"
            cat = model_catalogue.ModelCatalogue(config_path=path)
            entry = cat.add_client_key("persist-key-12345", name="n")
            cat2 = model_catalogue.ModelCatalogue(config_path=path)
            self.assertEqual(cat2.list_client_key_values(), ["persist-key-12345"])
            cat2.revoke_client_key(entry["id"])
            cat3 = model_catalogue.ModelCatalogue(config_path=path)
            self.assertEqual(cat3.list_client_key_values(), [])


    def test_add_client_key_autogenerates_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_config.json"
            cat = model_catalogue.ModelCatalogue(config_path=path)
            entry = cat.add_client_key("", name="auto")
            self.assertTrue(entry["key"].startswith("gb_"))
            self.assertGreaterEqual(len(entry["key"]), 8)
            self.assertEqual(cat.list_client_key_values(), [entry["key"]])

    def test_add_client_key_rejects_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = model_catalogue.ModelCatalogue(config_path=Path(tmp) / "admin_config.json")
            with self.assertRaises(ValueError):
                cat.add_client_key("short")


if __name__ == "__main__":
    unittest.main()
