//! Chromium / Electron `os_crypt` helpers (Windows DPAPI key + AES-256-GCM v10 blobs).
//!
//! Never log plaintext. Used only to import Grok Bot sand-secrets on the same Windows user.

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use serde_json::Value;
use thiserror::Error;
use zeroize::Zeroizing;

const V10_PREFIX: &[u8] = b"v10";
const DPAPI_PREFIX: &[u8] = b"DPAPI";
const NONCE_LEN: usize = 12;

#[derive(Debug, Error)]
pub enum OsCryptError {
    #[error("Local State 缺少 os_crypt.encrypted_key")]
    MissingEncryptedKey,
    #[error("encrypted_key 不是合法 Base64")]
    BadBase64,
    #[error("encrypted_key 缺少 DPAPI 前缀")]
    MissingDpapiPrefix,
    #[error("DPAPI 解密失败: {0}")]
    Dpapi(String),
    #[error("密文过短")]
    CiphertextTooShort,
    #[error("不支持的密文前缀（期望 v10）")]
    UnsupportedPrefix,
    #[error("AES-GCM 解密失败")]
    DecryptFailed,
    #[error("UTF-8 解码失败")]
    Utf8,
    #[error("IO: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("{0}")]
    Msg(String),
}

/// AES-256 key material for Chromium os_crypt (32 bytes).
pub type MasterKey = Zeroizing<[u8; 32]>;

/// Parse `%APPDATA%\\…\\Local State` and unwrap the DPAPI-protected master key.
pub fn load_master_key_from_local_state(local_state_path: &std::path::Path) -> Result<MasterKey, OsCryptError> {
    let text = std::fs::read_to_string(local_state_path)?;
    let json: Value = serde_json::from_str(&text)?;
    let b64 = json
        .pointer("/os_crypt/encrypted_key")
        .and_then(|v| v.as_str())
        .ok_or(OsCryptError::MissingEncryptedKey)?;
    let raw = B64.decode(b64).map_err(|_| OsCryptError::BadBase64)?;
    if raw.len() <= DPAPI_PREFIX.len() || &raw[..DPAPI_PREFIX.len()] != DPAPI_PREFIX {
        return Err(OsCryptError::MissingDpapiPrefix);
    }
    let protected = &raw[DPAPI_PREFIX.len()..];
    let unprotected = dpapi_unprotect(protected)?;
    if unprotected.len() != 32 {
        return Err(OsCryptError::Msg(format!(
            "master key 长度异常: {}（期望 32）",
            unprotected.len()
        )));
    }
    let mut key = Zeroizing::new([0u8; 32]);
    key.copy_from_slice(&unprotected);
    Ok(key)
}

/// Decrypt a Chromium `v10` + nonce + ciphertext||tag blob with the given master key.
pub fn decrypt_v10(master_key: &MasterKey, blob: &[u8]) -> Result<Vec<u8>, OsCryptError> {
    if blob.len() < V10_PREFIX.len() + NONCE_LEN + 16 {
        return Err(OsCryptError::CiphertextTooShort);
    }
    if &blob[..V10_PREFIX.len()] != V10_PREFIX {
        return Err(OsCryptError::UnsupportedPrefix);
    }
    let nonce_start = V10_PREFIX.len();
    let nonce = Nonce::from_slice(&blob[nonce_start..nonce_start + NONCE_LEN]);
    let ciphertext = &blob[nonce_start + NONCE_LEN..];
    let cipher =
        Aes256Gcm::new_from_slice(master_key.as_ref()).map_err(|_| OsCryptError::DecryptFailed)?;
    cipher
        .decrypt(nonce, ciphertext)
        .map_err(|_| OsCryptError::DecryptFailed)
}

pub fn decrypt_v10_string(master_key: &MasterKey, blob: &[u8]) -> Result<String, OsCryptError> {
    let plain = decrypt_v10(master_key, blob)?;
    String::from_utf8(plain).map_err(|_| OsCryptError::Utf8)
}

/// Decrypt a value that may be plain UTF-8, base64(v10…), or raw v10 bytes.
pub fn decrypt_secret_value(master_key: &MasterKey, value: &str) -> Result<String, OsCryptError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(OsCryptError::Msg("空密文".into()));
    }
    if looks_like_plaintext_secret(trimmed) {
        return Ok(trimmed.to_string());
    }
    if let Ok(raw) = B64.decode(trimmed) {
        if raw.starts_with(V10_PREFIX) {
            return decrypt_v10_string(master_key, &raw);
        }
    }
    if trimmed.as_bytes().starts_with(V10_PREFIX) {
        return decrypt_v10_string(master_key, trimmed.as_bytes());
    }
    Err(OsCryptError::UnsupportedPrefix)
}

fn looks_like_plaintext_secret(s: &str) -> bool {
    if s.len() == 36 && s.chars().filter(|c| *c == '-').count() == 4 {
        return true;
    }
    if s.starts_with("eyJ") && s.matches('.').count() >= 2 {
        return true;
    }
    false
}

#[cfg(windows)]
fn dpapi_unprotect(data: &[u8]) -> Result<Vec<u8>, OsCryptError> {
    use windows::Win32::Foundation::{LocalFree, HLOCAL};
    use windows::Win32::Security::Cryptography::{CryptUnprotectData, CRYPT_INTEGER_BLOB};

    let mut in_blob = CRYPT_INTEGER_BLOB {
        cbData: data.len() as u32,
        pbData: data.as_ptr() as *mut u8,
    };
    let mut out_blob = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: std::ptr::null_mut(),
    };
    let ok = unsafe {
        CryptUnprotectData(
            &mut in_blob,
            None,
            None,
            None,
            None,
            0,
            &mut out_blob,
        )
    };
    if ok.is_err() {
        return Err(OsCryptError::Dpapi("CryptUnprotectData 失败".into()));
    }
    let slice =
        unsafe { std::slice::from_raw_parts(out_blob.pbData, out_blob.cbData as usize) };
    let result = slice.to_vec();
    unsafe {
        let _ = LocalFree(HLOCAL(out_blob.pbData as *mut _));
    }
    Ok(result)
}

#[cfg(not(windows))]
fn dpapi_unprotect(_data: &[u8]) -> Result<Vec<u8>, OsCryptError> {
    Err(OsCryptError::Dpapi(
        "DPAPI 仅在 Windows 上可用（开发机可用单元测试中的 AES-GCM 向量）".into(),
    ))
}

/// Build a synthetic v10 blob for unit tests.
#[cfg(test)]
pub fn encrypt_v10_for_test(master_key: &MasterKey, plaintext: &[u8], nonce: [u8; 12]) -> Vec<u8> {
    let cipher = Aes256Gcm::new_from_slice(master_key.as_ref()).expect("key");
    let ct = cipher
        .encrypt(Nonce::from_slice(&nonce), plaintext)
        .expect("encrypt");
    let mut out = Vec::with_capacity(3 + 12 + ct.len());
    out.extend_from_slice(V10_PREFIX);
    out.extend_from_slice(&nonce);
    out.extend_from_slice(&ct);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn v10_roundtrip() {
        let key = Zeroizing::new([7u8; 32]);
        let nonce = [1u8, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
        let blob = encrypt_v10_for_test(&key, b"hello-os-crypt", nonce);
        let plain = decrypt_v10_string(&key, &blob).unwrap();
        assert_eq!(plain, "hello-os-crypt");
    }

    #[test]
    fn plaintext_uuid_passthrough() {
        let key = Zeroizing::new([9u8; 32]);
        let id = "d44e1d3d-04e5-43e7-b2b1-b3015c0b867c";
        assert_eq!(decrypt_secret_value(&key, id).unwrap(), id);
    }

    #[test]
    fn plaintext_jwt_passthrough() {
        let key = Zeroizing::new([9u8; 32]);
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig";
        assert_eq!(decrypt_secret_value(&key, jwt).unwrap(), jwt);
    }

    #[test]
    fn base64_v10_decrypt() {
        let key = Zeroizing::new([3u8; 32]);
        let nonce = [9u8; 12];
        let blob = encrypt_v10_for_test(&key, b"machine-id-value", nonce);
        let encoded = B64.encode(&blob);
        assert_eq!(
            decrypt_secret_value(&key, &encoded).unwrap(),
            "machine-id-value"
        );
    }
}
