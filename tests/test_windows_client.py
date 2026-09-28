"""Offline tests for the Windows client modules (safe on non-Windows)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.data = Path(self._tmpdir.name)

    def test_xor_roundtrip_and_environ(self) -> None:
        import windows.credentials as cred

        with mock.patch.object(cred, "app_data_dir", return_value=self.data):
            with mock.patch.object(cred, "_dpapi_available", return_value=False):
                with mock.patch.dict(
                    os.environ,
                    {
                        "HOME": self._tmpdir.name,
                        "USER": "testuser",
                        "USERNAME": "testuser",
                        "COMPUTERNAME": "testhost",
                        "HOSTNAME": "testhost",
                    },
                    clear=False,
                ):
                    os.environ.pop(cred.RENEWAL_ENV, None)
                    os.environ.pop(cred.API_KEY_ENV, None)
                    cred.set_renewal_credential("secret-value-do-not-log")
                    self.assertEqual(cred.get_renewal_credential(), "secret-value-do-not-log")
                    self.assertTrue(cred.has_renewal_credential())
                    self.assertEqual(cred.credential_status(), "set (stored)")
                    cred.set_api_key("proxy-key")
                    self.assertEqual(cred.get_api_key(), "proxy-key")
                    cred.apply_to_environ()
                    self.assertEqual(os.environ.get(cred.RENEWAL_ENV), "secret-value-do-not-log")
                    self.assertEqual(os.environ.get(cred.API_KEY_ENV), "proxy-key")
                    cred.set_renewal_credential("")
                    os.environ.pop(cred.RENEWAL_ENV, None)
                    self.assertFalse(cred.has_renewal_credential())

    def test_env_takes_precedence(self) -> None:
        import windows.credentials as cred

        with mock.patch.object(cred, "app_data_dir", return_value=self.data):
            with mock.patch.dict(os.environ, {cred.RENEWAL_ENV: "from-env"}, clear=False):
                self.assertEqual(cred.get_renewal_credential(), "from-env")
                self.assertEqual(cred.credential_status(), "set (env)")


class GatewayServiceImportTests(unittest.TestCase):
    def test_import_and_defaults(self) -> None:
        from windows.gateway_service import DEFAULT_HOST, DEFAULT_PORT, GatewayService

        self.assertEqual(DEFAULT_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_PORT, 8765)
        svc = GatewayService()
        self.assertFalse(svc.running)
        self.assertIn("Stopped", svc.status_text())
        self.assertEqual(svc.admin_url, "http://127.0.0.1:8765/admin")


class AppEntryTests(unittest.TestCase):
    def test_parse_args(self) -> None:
        from windows.app import parse_args

        args = parse_args(["--host", "127.0.0.1", "--port", "9001"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9001)

    def test_version_constant(self) -> None:
        import windows
        import grokbot2api

        self.assertEqual(windows.__version__, grokbot2api.__version__)


class TrayUiImportTests(unittest.TestCase):
    def test_import_client_app(self) -> None:
        from windows.tray_ui import ClientApp, APP_TITLE

        self.assertEqual(APP_TITLE, "grokbot2api")
        app = ClientApp()
        self.assertFalse(app.service.running)

    def test_tk_lazy(self) -> None:
        import windows.tray_ui as tray_ui

        # Module import must not require tkinter; helpers may raise at runtime.
        self.assertTrue(callable(tray_ui._require_tk))


@unittest.skipUnless(sys.platform == "win32", "DPAPI only on Windows")
class DpapiWindowsTests(unittest.TestCase):
    def test_dpapi_roundtrip(self) -> None:
        import windows.credentials as cred

        blob, method = cred._protect(b"hello-dpapi")
        self.assertEqual(method, "dpapi")
        self.assertEqual(cred._unprotect(blob, method), b"hello-dpapi")



class DesktopNavAndImportSourceTests(unittest.TestCase):
    """Static guards for Tauri client fixes (0.3.1 nav trap + 0.3.2 start/empty UX)."""

    def test_app_tsx_does_not_force_setup_on_every_poll(self) -> None:
        app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("initialNavApplied", app)
        # Must not gate poll refresh on has_renewal alone.
        self.assertNotIn("!c.onboarding_done && !c.has_renewal", app)
        self.assertIn('selectNav("setup")', app)  # banner / manual still OK

    def test_start_not_disabled_by_missing_renewal(self) -> None:
        app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertNotIn("disabled={busy || running || !creds?.has_renewal}", app)
        self.assertNotIn("disabled={busy || !creds?.has_renewal}", app)
        self.assertIn("emptyTitle", app)
        self.assertIn("emptyBody", app)
        types = (ROOT / "desktop" / "src" / "types.ts").read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "0.3.6"', types)

    def test_gateway_start_allows_missing_renewal(self) -> None:
        src = (ROOT / "desktop" / "src-tauri" / "src" / "gateway.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("renewal_warn", src)
        self.assertIn("missing_renewal", src)
        # Must not hard-return Err solely because renewal is missing.
        self.assertNotIn(
            "请在凭证向导中粘贴或导入后再启动",
            src,
        )
        self.assertIn("缺少推理续期凭证，网关已启动但推理调用会失败", src)

    def test_grok_import_handles_accounts_object_map(self) -> None:
        src = (ROOT / "desktop" / "src-tauri" / "src" / "grok_import.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("parse_accounts_object_map_prefers_active", src)
        self.assertIn("Some(Value::Object(acct_map))", src)
        self.assertIn("本机未存储会话 JWT", src)

if __name__ == "__main__":
    unittest.main()
