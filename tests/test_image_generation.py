"""Offline tests for AiService/RunGenerateImage + OpenAI images API."""

from __future__ import annotations

import base64
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
import image_gen  # noqa: E402
import model_catalogue  # noqa: E402
import sand_inference as upstream  # noqa: E402

# 1x1 PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class ProtobufRoundTripTests(unittest.TestCase):
    def test_encode_request_fields(self):
        raw = image_gen.encode_generate_image_request(
            "a fox",
            reference_images=[{"data": "abc", "mime_type": "image/png"}],
            model_id="ignored",
            max_mode=True,
            aspect_ratio="16:9",
        )
        fields, _ = upstream.pb_decode(raw)
        self.assertEqual(fields[1][0].decode(), "a fox")
        self.assertIn(2, fields)
        ref, _ = upstream.pb_decode(fields[2][0])
        self.assertEqual(ref[1][0].decode(), "abc")
        self.assertEqual(ref[2][0].decode(), "image/png")
        self.assertEqual(fields[3][0].decode(), "ignored")
        self.assertEqual(fields[4][0], 1)
        self.assertEqual(fields[5][0].decode(), "16:9")

    def test_decode_success_and_error(self):
        success_inner = upstream.pb_str(1, PNG_B64) + upstream.pb_str(2, "image/png")
        success_msg = upstream.pb_msg(1, success_inner)
        decoded = image_gen.decode_generate_image_response_proto(success_msg)
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["image_data"], PNG_B64)
        self.assertEqual(decoded["mime_type"], "image/png")

        err_inner = (
            upstream.pb_str(1, "nope")
            + upstream.pb_bool(2, True)
            + upstream.pb_var(3, 429)
            + upstream.pb_bool(4, True)
        )
        err_msg = upstream.pb_msg(2, err_inner)
        decoded_err = image_gen.decode_generate_image_response_proto(err_msg)
        self.assertFalse(decoded_err["ok"])
        self.assertEqual(decoded_err["error"], "nope")
        self.assertTrue(decoded_err["model_restricted"])
        self.assertEqual(decoded_err["provider_status_code"], 429)
        self.assertTrue(decoded_err["content_safety_blocked"])

    def test_json_encode_decode(self):
        body = image_gen.encode_generate_image_json(
            "cat", model_id="m", aspect_ratio="1:1", max_mode=True
        )
        payload = json.loads(body)
        self.assertEqual(payload["description"], "cat")
        self.assertEqual(payload["modelId"], "m")
        self.assertEqual(payload["aspectRatio"], "1:1")
        self.assertTrue(payload["maxMode"])

        raw = json.dumps(
            {"success": {"imageData": PNG_B64, "mimeType": "image/png"}}
        ).encode()
        decoded = image_gen.decode_generate_image_response_json(raw)
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["image_data"], PNG_B64)
        url_decoded = image_gen.decode_generate_image_response_json(
            {"success": {"imageUrl": "https://images.example.test/a.png", "mimeType": "image/png"}}
        )
        self.assertEqual(url_decoded["image_url"], "https://images.example.test/a.png")


class AspectRatioTests(unittest.TestCase):
    def test_size_mapping(self):
        self.assertEqual(image_gen.normalize_aspect_ratio(None, "1024x1024"), "1:1")
        self.assertEqual(image_gen.normalize_aspect_ratio(None, "1792x1024"), "16:9")
        self.assertEqual(image_gen.normalize_aspect_ratio("9:16", "1024x1024"), "9:16")

    def test_invalid_ratio(self):
        with self.assertRaises(ValueError):
            image_gen.normalize_aspect_ratio("2:1")


class TransportMockTests(unittest.TestCase):
    def test_run_generate_image_json_transport(self):
        seen = {}

        def fake_transport(url, body, headers):
            seen["url"] = url
            seen["body"] = json.loads(body)
            seen["headers"] = headers
            resp = json.dumps(
                {"success": {"imageData": PNG_B64, "mimeType": "image/png"}}
            ).encode()
            return 200, resp, "application/json"

        result = image_gen.run_generate_image(
            session_token="session-token",
            description="draw a fox",
            backend_url="https://example.test",
            meta={"x-cursor-client-type": "sand"},
            machine_id="machine",
            aspect_ratio="1:1",
            transport=fake_transport,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["image_data"], PNG_B64)
        self.assertIn("/aiserver.v1.AiService/RunGenerateImage", seen["url"])
        self.assertEqual(seen["body"]["description"], "draw a fox")
        self.assertEqual(seen["body"]["aspectRatio"], "1:1")
        self.assertTrue(seen["headers"]["authorization"].startswith("Bearer session-token"))
        self.assertIn("x-cursor-checksum", seen["headers"])
        self.assertEqual(seen["headers"]["x-cursor-client-type"], "sand")

    def test_http_error_raises(self):
        def fake_transport(url, body, headers):
            return 401, b'{"error":"nope"}', "application/json"

        with self.assertRaises(image_gen.ImageGenError) as ctx:
            image_gen.run_generate_image(
                session_token="t",
                description="x",
                machine_id="m",
                transport=fake_transport,
            )
        self.assertEqual(ctx.exception.http_status, 401)

    def test_upstream_error_payload(self):
        def fake_transport(url, body, headers):
            payload = {
                "error": {
                    "error": "blocked",
                    "contentSafetyBlocked": True,
                    "modelRestricted": False,
                }
            }
            return 200, json.dumps(payload).encode(), "application/json"

        with self.assertRaises(image_gen.ImageGenError) as ctx:
            image_gen.run_generate_image(
                session_token="t",
                description="x",
                machine_id="m",
                transport=fake_transport,
            )
        self.assertTrue(ctx.exception.content_safety_blocked)


class MediaStoreTests(unittest.TestCase):
    def test_raw_bytes_and_url_sources(self):
        expected = base64.b64decode(PNG_B64)
        raw, mime = image_gen.image_bytes_from_source(expected)
        self.assertEqual(raw, expected)
        self.assertIsNone(mime)

        seen = []
        def fetcher(url):
            seen.append(url)
            return expected, "image/png"

        raw, mime = image_gen.image_bytes_from_source(
            "https://images.example.test/generated/1", url_fetcher=fetcher
        )
        self.assertEqual(raw, expected)
        self.assertEqual(mime, "image/png")
        self.assertEqual(seen, ["https://images.example.test/generated/1"])

    def test_save_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = image_gen.MediaStore(Path(tmp))
            entry = store.save(PNG_B64, "image/png", prompt="fox", model="cursor-generate-image")
            self.assertTrue(entry["id"])
            path = store.resolve_path(entry["id"])
            self.assertIsNotNone(path)
            self.assertEqual(path.read_bytes(), base64.b64decode(PNG_B64))
            self.assertEqual(store.get(entry["id"])["prompt"], "fox")
            index = json.loads((Path(tmp) / "index.json").read_text())
            self.assertIn(entry["id"], index["items"])


class CatalogueImageModelTests(unittest.TestCase):
    def test_cursor_generate_image_capability(self):
        aliases = {m.alias: m for m in model_catalogue.BUILTIN_MODELS}
        self.assertIn("cursor-generate-image", aliases)
        spec = aliases["cursor-generate-image"]
        self.assertIn("image_generation", spec.capabilities)
        public = spec.to_public()
        self.assertIn("image_generation", public["capabilities"])


class FakeImageBackend:
    options = SimpleNamespace(model="grok-4.6")

    def __init__(self, media_dir: Path):
        self.media_store = image_gen.MediaStore(media_dir)
        self.last_prompt = ""

    def complete(self, model, messages, tools, request):
        return {"ok": True, "usage": {}}, "ok", []

    def sand_usage(self):
        return {"usagePercent": 0}

    def generate_image(
        self,
        prompt,
        *,
        model="cursor-generate-image",
        aspect_ratio=None,
        size=None,
        response_format="url",
        reference_images=None,
        max_mode=False,
        transport=None,
        media_store=None,
        public_base="",
    ):
        self.last_prompt = prompt
        ratio = image_gen.normalize_aspect_ratio(aspect_ratio, size)
        store = media_store or self.media_store
        saved = store.save(
            PNG_B64,
            "image/png",
            prompt=prompt,
            model=model,
            aspect_ratio=ratio,
        )
        url = f"{public_base.rstrip('/')}/media/{saved['id']}" if public_base else f"/media/{saved['id']}"
        payload = image_gen.openai_images_response(
            b64_json=PNG_B64,
            created=saved["created"],
            url=url,
            response_format=response_format,
        )
        return payload


class ImagesHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.backend = FakeImageBackend(Path(cls.tmp.name))
        cls.server = bridge.ProxyServer(("127.0.0.1", 0), cls.backend, "img-key")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def test_generations_requires_auth(self):
        req = urllib.request.Request(
            self.base_url + "/v1/images/generations",
            data=json.dumps({"prompt": "fox"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 401)

    def test_generations_and_media(self):
        payload = {
            "model": "cursor-generate-image",
            "prompt": "a watercolor fox",
            "aspect_ratio": "1:1",
            "response_format": "url",
        }
        req = urllib.request.Request(
            self.base_url + "/v1/images/generations",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer img-key",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            body = json.load(response)
        self.assertIn("data", body)
        self.assertEqual(self.backend.last_prompt, "a watercolor fox")
        self.assertEqual(set(body), {"created", "data"})
        url = body["data"][0]["url"]
        media_id = url.rsplit("/", 1)[-1]
        self.assertTrue(media_id)

        media_req = urllib.request.Request(
            self.base_url + f"/media/{media_id}",
            headers={"Authorization": "Bearer img-key"},
        )
        with urllib.request.urlopen(media_req, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("image/png", response.headers.get("Content-Type", ""))
            raw = response.read()
        self.assertEqual(raw, base64.b64decode(PNG_B64))

    def test_alias_path_and_b64(self):
        payload = {
            "prompt": "square",
            "size": "1024x1024",
            "response_format": "b64_json",
        }
        req = urllib.request.Request(
            self.base_url + "/images/generations",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer img-key",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            body = json.load(response)
        self.assertEqual(body["data"][0]["b64_json"], PNG_B64)
        self.assertEqual(set(body), {"created", "data"})

    def test_models_lists_image_model(self):
        req = urllib.request.Request(
            self.base_url + "/v1/models",
            headers={"Authorization": "Bearer img-key"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = json.load(response)
        ids = {item["id"]: item for item in payload["data"]}
        self.assertIn("cursor-generate-image", ids)
        self.assertIn("image_generation", ids["cursor-generate-image"].get("capabilities", []))


class SandBackendImageUnitTests(unittest.TestCase):
    def test_generate_image_uses_session_and_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = bridge.SandBackend.__new__(bridge.SandBackend)
            backend.options = SimpleNamespace(model="grok-4.6", max_mode=False)
            backend.lock = threading.Lock()
            backend.args = SimpleNamespace(
                backend_url="https://example.test",
                max_mode=False,
                timeout_ms=30000,
                credential="cred",
            )
            backend.media_store = image_gen.MediaStore(Path(tmp))
            backend.module = SimpleNamespace(
                load_renewal_credential=lambda args: "cred",
                client_meta=lambda args: {"x-cursor-client-type": "sand"},
                load_machine_id=lambda: "mid",
                renew=lambda credential, backend_url, meta: {
                    "accessToken": "grok-bot-token",
                    "sessionToken": "session-xyz",
                    "expiresAtMs": 1,
                    "renewed": True,
                },
                DEFAULT_BACKEND_URL="https://example.test",
            )

            def fake_transport(url, body, headers):
                self.assertIn("Bearer session-xyz", headers["authorization"])
                self.assertNotIn("grok-bot-token", headers["authorization"])
                payload = json.loads(body)
                self.assertEqual(payload["description"], "hello image")
                self.assertEqual(payload["aspectRatio"], "4:3")
                return (
                    200,
                    json.dumps(
                        {"success": {"imageData": PNG_B64, "mimeType": "image/png"}}
                    ).encode(),
                    "application/json",
                )

            result = backend.generate_image(
                "hello image",
                aspect_ratio="4:3",
                response_format="b64_json",
                transport=fake_transport,
                public_base="http://127.0.0.1:9",
            )
            self.assertEqual(result["data"][0]["b64_json"], PNG_B64)
            self.assertEqual(set(result), {"created", "data"})
            url_result = backend.generate_image(
                "hello image",
                aspect_ratio="4:3",
                response_format="url",
                transport=fake_transport,
                public_base="http://127.0.0.1:9",
            )
            self.assertIn("/media/", url_result["data"][0]["url"])


if __name__ == "__main__":
    unittest.main()
