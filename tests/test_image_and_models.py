import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_common  # noqa: E402
import grokbot2api as bridge  # noqa: E402
import model_catalogue  # noqa: E402
import sand_inference as upstream  # noqa: E402


class ImageEncodingTests(unittest.TestCase):
    def test_normalize_detects_data_url_image(self):
        content = [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                },
            },
        ]
        normalized = api_common.normalize_message_content(content)
        self.assertTrue(normalized["has_images"])
        self.assertEqual(normalized["text"], "describe")
        self.assertEqual(normalized["images"][0]["kind"], "base64")
        self.assertEqual(normalized["images"][0]["media_type"], "image/png")

    def test_encode_emits_content_parts_not_placeholder(self):
        content = [
            {"type": "text", "text": "what is this?"},
            {
                "type": "input_image",
                "image_url": "https://example.com/cat.png",
            },
        ]
        message = {"role": "user", "content": content}
        encoded = bridge.encode_native_message(upstream, message, {})
        fields, _ = upstream.pb_decode(encoded)
        self.assertNotIn(2, fields, "text field should not be used when images are present")
        self.assertIn(3, fields, "content parts field must be set")
        parts_msg, _ = upstream.pb_decode(fields[3][0])
        self.assertIn(1, parts_msg)
        # Ensure the omit-placeholder string is gone.
        blob = encoded
        self.assertNotIn(b"[image input omitted by local adapter]", blob)
        self.assertNotIn(b"[image]", blob)

    def test_text_only_stays_on_field_2(self):
        message = {"role": "user", "content": "hello"}
        encoded = bridge.encode_native_message(upstream, message, {})
        fields, _ = upstream.pb_decode(encoded)
        self.assertIn(2, fields)
        self.assertNotIn(3, fields)
        self.assertEqual(fields[2][0].decode(), "hello")


class ModelCatalogueTests(unittest.TestCase):
    def test_builtin_aliases_cover_requested_pool(self):
        aliases = {m.alias for m in model_catalogue.BUILTIN_MODELS}
        for needed in {
            "cursor-grok-4-7",
            "cursor-grok-4-7-fast",
            "cursor-grok-4-6",
            "cursor-grok-4-6-fast",
            "cursor-grok-4-5",
            "cursor-grok-4-5-fast",
            "cursor-composer-2-5",
            "cursor-composer-2-5-fast",
        }:
            self.assertIn(needed, aliases)

    def test_resolve_maps_fast_variant(self):
        catalogue = model_catalogue.ModelCatalogue(
            config_path=Path("/tmp/grokbot2api-test-admin-unused.json")
        )
        spec = catalogue.resolve("cursor-grok-4-6-fast")
        self.assertEqual(spec.upstream_id, "grok-4.6")
        self.assertIn(("fast", "true"), spec.params)

    def test_resolve_grok_4_7_and_fast(self):
        catalogue = model_catalogue.ModelCatalogue(
            config_path=Path("/tmp/grokbot2api-test-admin-unused-47.json")
        )
        self.assertEqual(model_catalogue.DEFAULT_ALIAS, "cursor-grok-4-7")
        std = catalogue.resolve("cursor-grok-4-7")
        self.assertEqual(std.upstream_id, "grok-4.7")
        self.assertIn(("fast", "false"), std.params)
        self.assertTrue(std.supports_vision)
        fast = catalogue.resolve("cursor-grok-4-7-fast")
        self.assertEqual(fast.upstream_id, "grok-4.7")
        self.assertIn(("fast", "true"), fast.params)
        bare = catalogue.resolve("grok-4.7")
        self.assertEqual(bare.upstream_id, "grok-4.7")
        self.assertEqual(catalogue.default_alias, "cursor-grok-4-7")

    def test_resolve_composer(self):
        catalogue = model_catalogue.ModelCatalogue(
            config_path=Path("/tmp/grokbot2api-test-admin-unused.json")
        )
        spec = catalogue.resolve("cursor-composer-2-5")
        self.assertEqual(spec.upstream_id, "composer-2.5")
        self.assertNotIn("effort", {k for k, _ in spec.params})


class ModelRoutingTests(unittest.TestCase):
    def test_client_alias_selects_mapped_upstream(self):
        backend = bridge.SandBackend.__new__(bridge.SandBackend)
        backend.options = SimpleNamespace(model="grok-4.6")
        backend.args = SimpleNamespace(model="", credential="credential")
        backend.lock = threading.Lock()
        backend.catalogue = model_catalogue.ModelCatalogue(
            config_path=Path("/tmp/grokbot2api-test-admin-unused2.json"),
            fallback_upstream="grok-4.6",
        )
        backend.module = SimpleNamespace(
            load_renewal_credential=lambda args: "credential",
            client_meta=lambda args: {},
            get_access_token=lambda args, credential, meta, force=False: {
                "accessToken": "token"
            },
        )
        seen = {}

        def fake_native_stream(module, args, token, messages, tools, request, model_params=None):
            seen["model"] = args.model
            seen["params"] = list(model_params or [])
            return {"ok": True, "model": args.model}

        with mock.patch.object(bridge, "native_stream_llm", side_effect=fake_native_stream):
            result = backend.infer_native("cursor-grok-4-5-fast", [], [], {})

        self.assertEqual(result["model"], "grok-4.5")
        self.assertEqual(seen["model"], "grok-4.5")
        self.assertIn(("fast", "true"), seen["params"])

    def test_omitted_model_uses_startup_upstream(self):
        backend = bridge.SandBackend.__new__(bridge.SandBackend)
        backend.options = SimpleNamespace(model="grok-4.6")
        backend.args = SimpleNamespace(model="", credential="credential")
        backend.lock = threading.Lock()
        backend.catalogue = model_catalogue.ModelCatalogue(
            default_alias="cursor-grok-4-5",
            config_path=Path("/tmp/grokbot2api-test-admin-unused3.json"),
            fallback_upstream="grok-4.6",
        )
        backend.module = SimpleNamespace(
            load_renewal_credential=lambda args: "credential",
            client_meta=lambda args: {},
            get_access_token=lambda args, credential, meta, force=False: {
                "accessToken": "token"
            },
        )

        def fake_native_stream(module, args, token, messages, tools, request, model_params=None):
            return {"ok": True, "model": args.model}

        with mock.patch.object(bridge, "native_stream_llm", side_effect=fake_native_stream):
            result = backend.infer_native("", [], [], {})

        self.assertEqual(result["model"], "grok-4.6")


class FakeBackend:
    options = SimpleNamespace(model="grok-4.6")

    def complete(self, model, messages, tools, request):
        return {"ok": True, "usage": {}}, "ok", []

    def sand_usage(self):
        return {"usagePercent": 1.0, "usageRemainingPercent": 99.0}


class ModelsAndAdminHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = bridge.ProxyServer(("127.0.0.1", 0), FakeBackend(), "")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_models_lists_full_catalogue(self):
        with urllib.request.urlopen(self.base_url + "/v1/models", timeout=3) as response:
            payload = json.load(response)
        ids = {item["id"] for item in payload["data"]}
        for needed in {
            "cursor-grok-4-7",
            "cursor-grok-4-7-fast",
            "cursor-grok-4-6",
            "cursor-grok-4-6-fast",
            "cursor-grok-4-5",
            "cursor-grok-4-5-fast",
            "cursor-composer-2-5",
            "cursor-composer-2-5-fast",
            "grok-4.7",
        }:
            self.assertIn(needed, ids)

    def test_admin_returns_200(self):
        with urllib.request.urlopen(self.base_url + "/admin", timeout=3) as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode()
        self.assertIn("工作台", body)
        self.assertIn("总览", body)
        self.assertIn("试用", body)


class AdminAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = bridge.ProxyServer(("127.0.0.1", 0), FakeBackend(), "secret-key")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_admin_requires_key(self):
        request = urllib.request.Request(self.base_url + "/admin")
        try:
            urllib.request.urlopen(request, timeout=3)
            self.fail("expected 401")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)

    def test_admin_accepts_bearer(self):
        request = urllib.request.Request(
            self.base_url + "/admin",
            headers={"Authorization": "Bearer secret-key"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
