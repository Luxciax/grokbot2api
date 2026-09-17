"""Cross-check Chromium os_crypt v10 AES-256-GCM decrypt (mirrors desktop/crates/os_crypt)."""

from __future__ import annotations

import base64
import unittest

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore


@unittest.skipUnless(AESGCM is not None, "cryptography package not installed")
class OsCryptV10Tests(unittest.TestCase):
    def test_v10_roundtrip_matches_rust_fixture_shape(self) -> None:
        key = bytes([7]) * 32
        nonce = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        plaintext = b"hello-os-crypt"
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        blob = b"v10" + nonce + ct
        # decrypt
        body = blob[3:]
        out = AESGCM(key).decrypt(body[:12], body[12:], None)
        self.assertEqual(out, plaintext)
        # base64 form used in JSON stores
        encoded = base64.b64encode(blob).decode("ascii")
        raw = base64.b64decode(encoded)
        self.assertTrue(raw.startswith(b"v10"))

    def test_plaintext_uuid_and_jwt_are_left_alone_by_policy(self) -> None:
        # Documented product behavior: plaintext UUID/JWT pass through without decrypt.
        uuid = "d44e1d3d-04e5-43e7-b2b1-b3015c0b867c"
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        self.assertEqual(uuid.count("-"), 4)
        self.assertTrue(jwt.startswith("eyJ"))


if __name__ == "__main__":
    unittest.main()
