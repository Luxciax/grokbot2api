"""Secure storage for Windows client secrets.

Prefer DPAPI (CryptProtectData / CryptUnprotectData) via ctypes when available.
Fall back to an obfuscated file under %APPDATA%\\grokbot2api. Never log secrets.
"""

from __future__ import annotations

import os
import sys
import base64
import hashlib
from pathlib import Path
from typing import Optional

APP_NAME = "grokbot2api"
RENEWAL_ENV = "SAND_INFERENCE_RENEWAL_CREDENTIAL"
API_KEY_ENV = "GROK_BUILD_PROXY_API_KEY"

_RENEWAL_FILE = "renewal_credential.dpapi"
_API_KEY_FILE = "proxy_api_key.dpapi"
_FALLBACK_SUFFIX = ".enc"


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / f".{APP_NAME}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_data_dir() -> Path:
    """Writable local state (token cache, admin_config, media)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / f".{APP_NAME}" / "local"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _protect_dpapi(plaintext: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_buf = ctypes.create_string_buffer(plaintext)
    blob_in = DATA_BLOB(len(plaintext), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptProtectData failed")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect_dpapi(ciphertext: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_buf = ctypes.create_string_buffer(ciphertext)
    blob_in = DATA_BLOB(len(ciphertext), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptUnprotectData failed")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _machine_key() -> bytes:
    """Weak local obfuscation key for non-Windows / DPAPI-unavailable fallback."""
    material = "|".join(
        [
            APP_NAME,
            sys.platform,
            os.environ.get("USERNAME") or os.environ.get("USER") or "user",
            os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "host",
            str(Path.home()),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _xor_obfuscate(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _protect(plaintext: bytes) -> tuple[bytes, str]:
    if _dpapi_available():
        try:
            return _protect_dpapi(plaintext), "dpapi"
        except OSError:
            pass
    return _xor_obfuscate(plaintext, _machine_key()), "xor"


def _unprotect(ciphertext: bytes, method: str) -> bytes:
    if method == "dpapi":
        return _unprotect_dpapi(ciphertext)
    return _xor_obfuscate(ciphertext, _machine_key())


def _store(name: str, value: str) -> None:
    raw = value.encode("utf-8")
    blob, method = _protect(raw)
    path = app_data_dir() / name
    payload = method.encode("ascii") + b"\n" + base64.b64encode(blob)
    path.write_bytes(payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load(name: str) -> Optional[str]:
    path = app_data_dir() / name
    if not path.is_file():
        # legacy fallback name
        alt = app_data_dir() / (name + _FALLBACK_SUFFIX)
        path = alt if alt.is_file() else path
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
        if b"\n" not in data:
            return None
        method_b, b64 = data.split(b"\n", 1)
        method = method_b.decode("ascii").strip()
        blob = base64.b64decode(b64.strip())
        return _unprotect(blob, method).decode("utf-8")
    except Exception:
        return None


def _clear(name: str) -> None:
    for candidate in (app_data_dir() / name, app_data_dir() / (name + _FALLBACK_SUFFIX)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def set_renewal_credential(value: str) -> None:
    value = (value or "").strip()
    if not value:
        _clear(_RENEWAL_FILE)
        return
    _store(_RENEWAL_FILE, value)


def get_renewal_credential() -> Optional[str]:
    env = os.environ.get(RENEWAL_ENV, "").strip()
    if env:
        return env
    return _load(_RENEWAL_FILE)


def set_api_key(value: str) -> None:
    value = (value or "").strip()
    if not value:
        _clear(_API_KEY_FILE)
        return
    _store(_API_KEY_FILE, value)


def get_api_key() -> Optional[str]:
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return env
    return _load(_API_KEY_FILE)


def apply_to_environ() -> None:
    """Export stored secrets into process env for the embedded gateway. Never log values."""
    renewal = get_renewal_credential()
    if renewal:
        os.environ[RENEWAL_ENV] = renewal
    api_key = get_api_key()
    if api_key:
        os.environ[API_KEY_ENV] = api_key


def has_renewal_credential() -> bool:
    return bool(get_renewal_credential())


def credential_status() -> str:
    """Human-readable status without revealing the secret."""
    if os.environ.get(RENEWAL_ENV, "").strip():
        return "set (env)"
    if _load(_RENEWAL_FILE):
        return "set (stored)"
    return "missing"
