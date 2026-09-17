//! Persist secrets without returning them to the frontend.
//! Windows: Credential Manager via `keyring`. Fallback / non-Windows: DPAPI-style file under app data
//! is not available off-Windows; we use a machine-scoped XOR file (dev only) under app data.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use thiserror::Error;
use zeroize::Zeroize;

const SERVICE: &str = "grokbot2api";
const KEY_RENEWAL: &str = "renewal_credential";
const KEY_API: &str = "proxy_api_key";
const KEY_ACCESS: &str = "session_access_token";
const KEY_REFRESH: &str = "session_refresh_token";
const KEY_MACHINE: &str = "machine_id";

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("{0}")]
    Msg(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AppSettings {
    pub host: String,
    pub port: u16,
    pub machine_id: String,
    pub onboarding_done: bool,
    /// Last known profile email from import (non-secret).
    pub profile_email: String,
}

impl AppSettings {
    pub fn default_settings() -> Self {
        Self {
            host: "127.0.0.1".into(),
            port: 18765,
            machine_id: String::new(),
            onboarding_done: false,
            profile_email: String::new(),
        }
    }
}

pub fn app_data_dir() -> Result<PathBuf, StoreError> {
    let base = dirs::data_dir().ok_or_else(|| StoreError::Msg("无法解析 AppData".into()))?;
    let path = base.join("com.luxciax.grokbot2api");
    std::fs::create_dir_all(&path).map_err(|e| StoreError::Msg(e.to_string()))?;
    Ok(path)
}

pub fn local_data_dir() -> Result<PathBuf, StoreError> {
    let base = dirs::data_local_dir()
        .or_else(dirs::data_dir)
        .ok_or_else(|| StoreError::Msg("无法解析 LocalAppData".into()))?;
    let path = base.join("com.luxciax.grokbot2api");
    std::fs::create_dir_all(&path).map_err(|e| StoreError::Msg(e.to_string()))?;
    Ok(path)
}

pub fn settings_path() -> Result<PathBuf, StoreError> {
    Ok(app_data_dir()?.join("settings.json"))
}

pub fn load_settings() -> AppSettings {
    let Ok(path) = settings_path() else {
        return AppSettings::default_settings();
    };
    match std::fs::read_to_string(&path) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_else(|_| AppSettings::default_settings()),
        Err(_) => AppSettings::default_settings(),
    }
}

pub fn save_settings(settings: &AppSettings) -> Result<(), StoreError> {
    let path = settings_path()?;
    let text = serde_json::to_string_pretty(settings).map_err(|e| StoreError::Msg(e.to_string()))?;
    std::fs::write(path, text).map_err(|e| StoreError::Msg(e.to_string()))
}

fn set_secret(key: &str, value: &str) -> Result<(), StoreError> {
    let value = value.trim();
    if value.is_empty() {
        return clear_secret(key);
    }
    #[cfg(windows)]
    {
        let entry = keyring::Entry::new(SERVICE, key).map_err(|e| StoreError::Msg(e.to_string()))?;
        entry
            .set_password(value)
            .map_err(|e| StoreError::Msg(e.to_string()))?;
        return Ok(());
    }
    #[cfg(not(windows))]
    {
        file_set_secret(key, value)
    }
}

fn get_secret(key: &str) -> Option<String> {
    #[cfg(windows)]
    {
        let entry = keyring::Entry::new(SERVICE, key).ok()?;
        return entry.get_password().ok().filter(|s| !s.is_empty());
    }
    #[cfg(not(windows))]
    {
        file_get_secret(key)
    }
}

fn clear_secret(key: &str) -> Result<(), StoreError> {
    #[cfg(windows)]
    {
        if let Ok(entry) = keyring::Entry::new(SERVICE, key) {
            let _ = entry.delete_credential();
        }
        return Ok(());
    }
    #[cfg(not(windows))]
    {
        file_clear_secret(key)
    }
}

#[cfg(not(windows))]
fn secrets_file() -> Result<PathBuf, StoreError> {
    Ok(app_data_dir()?.join("secrets.obf"))
}

#[cfg(not(windows))]
fn file_set_secret(key: &str, value: &str) -> Result<(), StoreError> {
    use std::collections::HashMap;
    let path = secrets_file()?;
    let mut map: HashMap<String, String> = if path.is_file() {
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_default()).unwrap_or_default()
    } else {
        HashMap::new()
    };
    map.insert(key.to_string(), obfuscate(value));
    let text = serde_json::to_string(&map).map_err(|e| StoreError::Msg(e.to_string()))?;
    std::fs::write(path, text).map_err(|e| StoreError::Msg(e.to_string()))
}

#[cfg(not(windows))]
fn file_get_secret(key: &str) -> Option<String> {
    use std::collections::HashMap;
    let path = secrets_file().ok()?;
    let map: HashMap<String, String> =
        serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()?;
    let enc = map.get(key)?;
    deobfuscate(enc)
}

#[cfg(not(windows))]
fn file_clear_secret(key: &str) -> Result<(), StoreError> {
    use std::collections::HashMap;
    let path = secrets_file()?;
    if !path.is_file() {
        return Ok(());
    }
    let mut map: HashMap<String, String> =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_default()).unwrap_or_default();
    map.remove(key);
    let text = serde_json::to_string(&map).map_err(|e| StoreError::Msg(e.to_string()))?;
    std::fs::write(path, text).map_err(|e| StoreError::Msg(e.to_string()))
}

#[cfg(not(windows))]
fn obfuscate(value: &str) -> String {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
    let key = b"grokbot2api-dev-only";
    let xored: Vec<u8> = value
        .as_bytes()
        .iter()
        .enumerate()
        .map(|(i, b)| b ^ key[i % key.len()])
        .collect();
    B64.encode(xored)
}

#[cfg(not(windows))]
fn deobfuscate(value: &str) -> Option<String> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
    let key = b"grokbot2api-dev-only";
    let raw = B64.decode(value).ok()?;
    let plain: Vec<u8> = raw
        .iter()
        .enumerate()
        .map(|(i, b)| b ^ key[i % key.len()])
        .collect();
    String::from_utf8(plain).ok()
}

pub fn set_renewal_credential(value: &str) -> Result<(), StoreError> {
    set_secret(KEY_RENEWAL, value)
}

pub fn get_renewal_credential() -> Option<String> {
    std::env::var("SAND_INFERENCE_RENEWAL_CREDENTIAL")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| get_secret(KEY_RENEWAL))
}

pub fn set_api_key(value: &str) -> Result<(), StoreError> {
    set_secret(KEY_API, value)
}

pub fn get_api_key() -> Option<String> {
    std::env::var("GROK_BUILD_PROXY_API_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| get_secret(KEY_API))
}

pub fn set_session_tokens(access: Option<&str>, refresh: Option<&str>) -> Result<(), StoreError> {
    if let Some(a) = access {
        set_secret(KEY_ACCESS, a)?;
    }
    if let Some(r) = refresh {
        set_secret(KEY_REFRESH, r)?;
    }
    Ok(())
}

pub fn has_access_token() -> bool {
    get_secret(KEY_ACCESS).is_some()
}

pub fn has_refresh_token() -> bool {
    get_secret(KEY_REFRESH).is_some()
}

pub fn get_access_token() -> Option<String> {
    get_secret(KEY_ACCESS)
}

pub fn clear_session_tokens() -> Result<(), StoreError> {
    clear_secret(KEY_ACCESS)?;
    clear_secret(KEY_REFRESH)?;
    Ok(())
}

pub fn set_machine_id_secret(value: &str) -> Result<(), StoreError> {
    set_secret(KEY_MACHINE, value)?;
    let mut s = load_settings();
    s.machine_id = value.trim().to_string();
    save_settings(&s)
}

pub fn get_machine_id() -> Option<String> {
    let from_settings = load_settings().machine_id;
    if !from_settings.trim().is_empty() {
        return Some(from_settings);
    }
    get_secret(KEY_MACHINE)
}

pub fn has_renewal() -> bool {
    get_renewal_credential().is_some()
}

/// Apply secrets into a process environment map (for gateway child). Values are moved; avoid logging.
pub fn apply_secrets_to_env(env: &mut std::collections::HashMap<String, String>) {
    if let Some(mut r) = get_renewal_credential() {
        env.insert("SAND_INFERENCE_RENEWAL_CREDENTIAL".into(), r.clone());
        r.zeroize();
    }
    if let Some(mut k) = get_api_key() {
        env.insert("GROK_BUILD_PROXY_API_KEY".into(), k.clone());
        k.zeroize();
    }
    if let Some(mid) = get_machine_id() {
        env.insert("SAND_MACHINE_ID".into(), mid.clone());
        env.insert("GROKBOT_MACHINE_ID".into(), mid);
    }
    // Session JWTs for Dashboard / AiService (image gen); NOT for InferenceService.
    if let Some(mut a) = get_access_token() {
        env.insert("SAND_SESSION_TOKEN".into(), a.clone());
        env.insert("CURSOR_SESSION_TOKEN".into(), a.clone());
        env.insert("GROKBOT_SESSION_ACCESS_TOKEN".into(), a.clone());
        a.zeroize();
    }
    if let Some(mut r) = get_secret(KEY_REFRESH) {
        env.insert("SAND_SESSION_REFRESH_TOKEN".into(), r.clone());
        env.insert("CURSOR_SESSION_REFRESH_TOKEN".into(), r.clone());
        r.zeroize();
    }
}
