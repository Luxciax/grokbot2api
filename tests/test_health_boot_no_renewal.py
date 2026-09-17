"""Gateway must serve /health and /admin without SAND_INFERENCE_RENEWAL_CREDENTIAL."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HealthBootWithoutRenewalTests(unittest.TestCase):
    def test_process_serves_fingerprint_health_without_sbi(self):
        env = os.environ.copy()
        env.pop("SAND_INFERENCE_RENEWAL_CREDENTIAL", None)
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "grokbot2api.py"),
                "--listen",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        health = None
        last_err = None
        for _ in range(50):
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                self.fail(f"gateway exited early code={proc.returncode}: {err[:800]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    health = json.load(resp)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.2)
        self.assertIsNotNone(health, f"health not ready: {last_err}")
        assert health is not None
        self.assertTrue(health.get("ok"))
        self.assertEqual(health.get("service"), "grokbot2api")
        self.assertIn("version", health)
        self.assertEqual(health.get("admin"), "/admin")
        self.assertFalse(health.get("renewal_configured"))

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/admin", timeout=3) as resp:
            html = resp.read().decode("utf-8", "replace")
        self.assertIn("工作台", html)
        self.assertNotIn("Unknown endpoint", html)


class HealthFingerprintUnitTests(unittest.TestCase):
    def test_version_bumped(self):
        import grokbot2api as bridge

        self.assertEqual(bridge.__version__, "0.3.3")


if __name__ == "__main__":
    unittest.main()
