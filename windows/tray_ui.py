"""System tray UI with graceful fallback to a small Tk control window."""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Callable, Optional

from windows.credentials import (
    API_KEY_ENV,
    credential_status,
    get_api_key,
    has_renewal_credential,
    set_api_key,
    set_renewal_credential,
)
from windows.gateway_service import GatewayService

if TYPE_CHECKING:
    import tkinter as tk

APP_TITLE = "grokbot2api"


def _require_tk():
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on OS build
        raise RuntimeError(
            "tkinter is required for the Windows client UI. "
            "Install a Python build that includes Tk (standard on Windows CPython)."
        ) from exc
    return tk, messagebox, simpledialog


def _make_icon_image():
    """Create a small RGBA icon without requiring Pillow at runtime for logic paths."""
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, 60, 60), fill=(32, 128, 96, 255))
        draw.rectangle((22, 18, 42, 46), fill=(240, 240, 240, 255))
        return img
    except Exception:
        try:
            from PIL import Image

            return Image.new("RGBA", (16, 16), (32, 128, 96, 255))
        except Exception:
            return None


class ClientApp:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.service = GatewayService(host=host, port=port, on_status=self._on_status)
        self._root: Optional["tk.Tk"] = None
        self._status_var = None
        self._tray = None
        self._tray_thread: Optional[threading.Thread] = None
        self._use_tray = False
        self._messagebox = None
        self._simpledialog = None

    def _ensure_tk_helpers(self) -> None:
        if self._messagebox is None:
            _tk, messagebox, simpledialog = _require_tk()
            self._messagebox = messagebox
            self._simpledialog = simpledialog

    def _on_status(self, message: str) -> None:
        def _apply() -> None:
            if self._status_var is not None:
                self._status_var.set(message)
            if self._tray is not None:
                try:
                    self._tray.title = f"{APP_TITLE}: {message}"
                except Exception:
                    pass

        if self._root is not None:
            try:
                self._root.after(0, _apply)
                return
            except Exception:
                pass
        _apply()

    def start_gateway(self) -> None:
        self._ensure_tk_helpers()
        assert self._messagebox is not None
        if not has_renewal_credential():
            self._prompt_credential(required=True)
            if not has_renewal_credential():
                self._messagebox.showwarning(
                    APP_TITLE, "Renewal credential is required to start the gateway."
                )
                return
        try:
            self.service.start()
        except Exception as exc:  # noqa: BLE001
            self._messagebox.showerror(APP_TITLE, f"Failed to start gateway:\n{exc}")

    def stop_gateway(self) -> None:
        self._ensure_tk_helpers()
        assert self._messagebox is not None
        try:
            self.service.stop()
        except Exception as exc:  # noqa: BLE001
            self._messagebox.showerror(APP_TITLE, f"Failed to stop gateway:\n{exc}")

    def open_admin(self) -> None:
        import webbrowser

        self._ensure_tk_helpers()
        assert self._messagebox is not None
        if not self.service.running:
            if self._messagebox.askyesno(APP_TITLE, "Gateway is not running. Start it now?"):
                self.start_gateway()
            else:
                return
        webbrowser.open(self.service.admin_url)

    def _prompt_credential(self, required: bool = False) -> None:
        self._ensure_tk_helpers()
        assert self._simpledialog is not None
        prompt = "Cursor renewal credential (SAND_INFERENCE_RENEWAL_CREDENTIAL):"
        if not required:
            prompt += "\n(Leave empty to clear stored value.)"
        value = self._simpledialog.askstring(APP_TITLE, prompt, show="*", parent=self._root)
        if value is None:
            return
        set_renewal_credential(value)
        self._refresh_labels()

    def _prompt_api_key(self) -> None:
        self._ensure_tk_helpers()
        assert self._simpledialog is not None
        assert self._messagebox is not None
        prompt = f"Optional local proxy API key ({API_KEY_ENV}):\n(Leave empty to clear.)"
        value = self._simpledialog.askstring(APP_TITLE, prompt, show="*", parent=self._root)
        if value is None:
            return
        set_api_key(value)
        self._refresh_labels()
        self._messagebox.showinfo(
            APP_TITLE,
            "API key saved. Restart the gateway if it is already running for the change to apply.",
        )

    def _refresh_labels(self) -> None:
        if self._status_var is not None:
            cred = credential_status()
            key = "set" if get_api_key() else "none"
            base = self.service.status_text()
            self._status_var.set(f"{base} | credential: {cred} | api key: {key}")

    def quit_app(self) -> None:
        try:
            self.service.stop()
        except Exception:
            pass
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass

    def _build_tk_window(self, *, as_fallback: bool):
        tk, _messagebox, _simpledialog = _require_tk()
        self._messagebox = _messagebox
        self._simpledialog = _simpledialog

        root = tk.Tk()
        root.title(APP_TITLE)
        root.geometry("420x220")
        root.resizable(False, False)

        self._status_var = tk.StringVar(value=self.service.status_text())
        frm = tk.Frame(root, padx=12, pady=12)
        frm.pack(fill=tk.BOTH, expand=True)

        tk.Label(frm, text="Local grokbot2api gateway", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(frm, textvariable=self._status_var, wraplength=390, justify=tk.LEFT).pack(
            anchor="w", pady=(8, 12)
        )

        row = tk.Frame(frm)
        row.pack(fill=tk.X)
        tk.Button(row, text="Start", width=10, command=self.start_gateway).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text="Stop", width=10, command=self.stop_gateway).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text="Open Admin", width=12, command=self.open_admin).pack(side=tk.LEFT, padx=2)

        row2 = tk.Frame(frm)
        row2.pack(fill=tk.X, pady=(8, 0))
        tk.Button(row2, text="Set Credential", width=14, command=lambda: self._prompt_credential(False)).pack(
            side=tk.LEFT, padx=2
        )
        tk.Button(row2, text="Set API Key", width=12, command=self._prompt_api_key).pack(side=tk.LEFT, padx=2)
        tk.Button(row2, text="Quit", width=10, command=self.quit_app).pack(side=tk.LEFT, padx=2)

        if as_fallback:
            tk.Label(
                frm,
                text="Tray icon unavailable — using this control window.",
                fg="#666",
            ).pack(anchor="w", pady=(12, 0))

        root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self._root = root
        self._refresh_labels()
        return root

    def _try_start_tray(self) -> bool:
        try:
            import pystray
            from pystray import MenuItem as Item
        except Exception:
            return False

        icon_image = _make_icon_image()
        if icon_image is None:
            return False

        def _run_on_ui(fn: Callable[[], None]) -> None:
            if self._root is not None:
                self._root.after(0, fn)
            else:
                fn()

        menu = pystray.Menu(
            Item("Start", lambda: _run_on_ui(self.start_gateway)),
            Item("Stop", lambda: _run_on_ui(self.stop_gateway)),
            Item("Open Admin", lambda: _run_on_ui(self.open_admin)),
            Item("Set Credential", lambda: _run_on_ui(lambda: self._prompt_credential(False))),
            Item("Set API Key", lambda: _run_on_ui(self._prompt_api_key)),
            Item("Quit", lambda: _run_on_ui(self.quit_app)),
        )
        self._tray = pystray.Icon(APP_TITLE, icon_image, APP_TITLE, menu)

        def _tray_run() -> None:
            assert self._tray is not None
            self._tray.run()

        self._tray_thread = threading.Thread(target=_tray_run, name="grokbot2api-tray", daemon=True)
        self._tray_thread.start()
        self._use_tray = True
        return True

    def run(self) -> None:
        tray_ok = False
        if sys.platform == "win32":
            tray_ok = self._try_start_tray()

        if tray_ok:
            root = self._build_tk_window(as_fallback=False)
            root.withdraw()
            root.mainloop()
        else:
            root = self._build_tk_window(as_fallback=True)
            root.mainloop()
