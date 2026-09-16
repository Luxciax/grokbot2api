"""Start / stop the embedded local grokbot2api HTTP gateway."""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

# Repo root (parent of windows/) must be on sys.path for gateway imports.
_WINDOWS_DIR = Path(__file__).resolve().parent
_ROOT = _WINDOWS_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from windows.credentials import (  # noqa: E402
    API_KEY_ENV,
    apply_to_environ,
    local_data_dir,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _resource_root() -> Path:
    """Directory containing bundled gateway modules (dev or PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return _ROOT


class GatewayService:
    """Runs grokbot2api.ProxyServer in a background thread."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.on_status = on_status or (lambda _msg: None)
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._error: Optional[str] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._server is not None

    @property
    def last_error(self) -> Optional[str]:
        return self._error

    def status_text(self) -> str:
        if self.running:
            return f"Running on {self.base_url}"
        if self._error:
            return f"Stopped ({self._error})"
        return "Stopped"

    def start(self) -> None:
        with self._lock:
            if self.running:
                self.on_status(self.status_text())
                return
            self._error = None
            apply_to_environ()
            try:
                self._server = self._create_server()
            except Exception as exc:  # noqa: BLE001 — surface to UI
                self._error = str(exc)
                self._server = None
                self.on_status(self.status_text())
                raise

            server = self._server

            def _run() -> None:
                try:
                    server.serve_forever(poll_interval=0.5)
                except Exception as exc:  # noqa: BLE001
                    self._error = str(exc)
                finally:
                    self.on_status(self.status_text())

            self._thread = threading.Thread(target=_run, name="grokbot2api-gateway", daemon=True)
            self._thread.start()

        # Brief wait so bind failures surface quickly.
        time.sleep(0.15)
        if self._error:
            self.on_status(self.status_text())
            raise RuntimeError(self._error)
        self.on_status(self.status_text())

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self.on_status(self.status_text())

    def _create_server(self) -> Any:
        root = _resource_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        import grokbot2api as bridge
        from model_catalogue import DEFAULT_ALIAS

        data = local_data_dir()
        media_dir = data / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        upstream = root / "sand_inference.py"
        if not upstream.is_file():
            # onedir layout: modules next to the executable
            exe_dir = Path(sys.executable).resolve().parent
            alt = exe_dir / "sand_inference.py"
            if alt.is_file():
                upstream = alt
                root = exe_dir

        options = SimpleNamespace(
            listen=self.host,
            port=self.port,
            model="grok-4.6",
            default_alias=DEFAULT_ALIAS,
            admin_config=data / "admin_config.json",
            media_dir=media_dir,
            upstream_script=upstream,
            backend_url="",
            cache=data / "token-cache.json",
            max_mode=False,
            conversation_id="",
            client_type="sand",
            client_version="0.30.0",
            namespace="prod",
            team_id="",
            timeout_ms=600_000,
            min_max_tokens=getattr(bridge, "DEFAULT_MIN_MAX_TOKENS", 160),
            api_key_env=API_KEY_ENV,
            cors_origins="",
        )

        if options.listen not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(
            options.api_key_env, ""
        ):
            raise RuntimeError("refusing non-loopback listen without a proxy API key")

        catalogue = bridge.ModelCatalogue(
            default_alias=options.default_alias,
            config_path=options.admin_config,
            fallback_upstream=options.model,
        )
        backend = bridge.SandBackend(options, catalogue=catalogue)
        api_key = os.environ.get(options.api_key_env, "")
        cors_origins = [o.strip() for o in str(options.cors_origins or "").split(",") if o.strip()]
        return bridge.ProxyServer(
            (options.listen, options.port),
            backend,
            api_key,
            cors_origins=cors_origins,
        )

    def wait_until_ready(self, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        url = f"{self.base_url}/health"
        while time.time() < deadline:
            if self._error:
                return False
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if 200 <= getattr(resp, "status", 200) < 300:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(0.2)
        return False
