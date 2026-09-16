"""Admin workbench APIs: custom models, media list/delete."""

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


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeBackend:
    options = SimpleNamespace(model="grok-4.6")

    def __init__(self, media_dir: Path):
        self.media_store = image_gen.MediaStore(media_dir)

    def complete(self, model, messages, tools, request):
        return {"ok": True, "usage": {}}, "ok", []

    def sand_usage(self):
        return {"usagePercent": 2.5, "usageRemainingPercent": 97.5}


class CustomModelPersistenceTests(unittest.TestCase):
    def test_custom_alias_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_config.json"
            cat = model_catalogue.ModelCatalogue(config_path=path)
            spec = cat.add_custom_model(
                {
                    "alias": "my-grok-lab",
                    "upstream_id": "grok-4.6",
                    "params": [{"id": "effort", "value": "high"}, {"id": "fast", "value": "true"}],
                    "capabilities": ["chat", "vision"],
                    "display_name": "Lab Grok",
                }
            )
            self.assertTrue(spec.is_custom)
            self.assertIn("vision", spec.capabilities)
            cat2 = model_catalogue.ModelCatalogue(config_path=path)
            self.assertIn("my-grok-lab", cat2.models)
            self.assertTrue(cat2.models["my-grok-lab"].is_custom)
            self.assertEqual(cat2.models["my-grok-lab"].upstream_id, "grok-4.6")
            public = {m["id"] for m in cat2.list_public()}
            self.assertIn("my-grok-lab", public)

    def test_reject_invalid_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = model_catalogue.ModelCatalogue(config_path=Path(tmp) / "admin_config.json")
            with self.assertRaises(ValueError):
                cat.add_custom_model({"alias": "../evil", "upstream_id": "grok-4.6"})


class AdminWorkbenchHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        media_dir = Path(cls.tmp.name) / "media"
        config_path = Path(cls.tmp.name) / "admin_config.json"
        catalogue = model_catalogue.ModelCatalogue(config_path=config_path)
        backend = FakeBackend(media_dir)
        cls.server = bridge.ProxyServer(("127.0.0.1", 0), backend, "wb-secret-key")
        cls.server.catalogue = catalogue
        cls.server.media_store = backend.media_store
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.auth = {"Authorization": "Bearer wb-secret-key"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json", **self.auth, **(headers or {})}
        req = urllib.request.Request(self.base_url + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                body = response.read().decode()
                return response.status, json.loads(body) if body else {}, dict(response.headers)
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return error.code, parsed, dict(error.headers)

    def test_admin_workbench_html_sections(self):
        req = urllib.request.Request(self.base_url + "/admin", headers=self.auth)
        with urllib.request.urlopen(req, timeout=3) as response:
            html = response.read().decode()
            self.assertEqual(response.status, 200)
        for needle in ("总览", "模型", "密钥", "审计", "媒体", "试用", "设置", "navbtn"):
            self.assertIn(needle, html)

    def test_status_includes_media_count(self):
        status, payload, _ = self.request("GET", "/admin/api/status")
        self.assertEqual(status, 200)
        self.assertIn("media_count", payload)
        self.assertIn("uptime_seconds", payload)

    def test_add_custom_model_via_api(self):
        status, payload, _ = self.request(
            "POST",
            "/admin/api/models",
            {
                "action": "add",
                "alias": "wb-custom-1",
                "upstream_id": "grok-4.5",
                "params": {"effort": "high", "fast": "false"},
                "capabilities": ["chat"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        ids = {m["id"] for m in payload["catalogue"]["models"]}
        self.assertIn("wb-custom-1", ids)
        added = next(m for m in payload["catalogue"]["models"] if m["id"] == "wb-custom-1")
        self.assertTrue(added.get("is_custom"))

        # appears in public models list
        status, models, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn("wb-custom-1", {m["id"] for m in models["data"]})

    def test_media_list_and_delete(self):
        saved = self.server.media_store.save(
            PNG_B64,
            "image/png",
            prompt="tiny pixel",
            model="cursor-generate-image",
            aspect_ratio="1:1",
        )
        media_id = saved["id"]
        status, payload, _ = self.request("GET", "/admin/api/media")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["count"], 1)
        ids = {item["id"] for item in payload["items"]}
        self.assertIn(media_id, ids)

        status, deleted, _ = self.request("DELETE", f"/admin/api/media/{media_id}")
        self.assertEqual(status, 200)
        self.assertTrue(deleted.get("ok"))
        self.assertEqual(deleted.get("deleted"), media_id)
        self.assertIsNone(self.server.media_store.get(media_id))

        status, payload, _ = self.request("GET", "/admin/api/media")
        self.assertEqual(status, 200)
        self.assertNotIn(media_id, {item["id"] for item in payload["items"]})

    def test_media_delete_requires_auth(self):
        saved = self.server.media_store.save(PNG_B64, "image/png", prompt="x")
        req = urllib.request.Request(
            self.base_url + f"/admin/api/media/{saved['id']}",
            method="DELETE",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
