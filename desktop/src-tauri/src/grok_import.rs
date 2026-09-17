//! Import credentials from the installed Grok Bot Electron app.
//!
//! Verified layout (Windows `%APPDATA%\Grok Bot\`):
//!   - `Local State` JSON: `os_crypt.encrypted_key` = base64(`DPAPI` + blob);
//!     CryptUnprotectData(blob) → 32-byte AES key (Chromium OSCrypt).
//!   - `sand-secrets.json`:
//!       - `cursor-machine-id`: base64 v10 blob → UTF-8 machine id (UUID)
//!       - `cursor-accounts`: **plaintext JSON** `{active, accounts}` where each
//!         account has `cursor-access-token` / `cursor-refresh-token` whose *values*
//!         are base64 `v10`+nonce+ciphertext AES-GCM with the OSCrypt key
//!       - `local-exec-file-key`: same v10 style (ignored here)
//!
//! What we import:
//!   1. machine id + session JWTs (Dashboard / AiService image path)
//!   2. best-effort scan for classic `sbi_…` renewal credential in known files
//!   3. secondary: `%USERPROFILE%\.grokbot\` and `gateway-descriptor.json`
//!
//! Session JWTs (`type: session`, `aud: https://cursor.com`) are **not**
//! `SAND_INFERENCE_RENEWAL_CREDENTIAL`. Never log plaintext secrets.

use crate::os_crypt::{decrypt_secret_value, load_master_key_from_local_state, MasterKey};
use crate::secure_store::{self, StoreError};
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Debug, Serialize)]
pub struct ImportResult {
    pub machine_id: Option<String>,
    pub has_access_token: bool,
    pub has_refresh_token: bool,
    pub profile_email: Option<String>,
    pub errors: Vec<String>,
    /// True only if an `sbi_…` (or similar) renewal string was found & saved.
    pub inference_renewal_available: bool,
    pub note: String,
}

fn grok_bot_dirs() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Some(appdata) = dirs::data_dir() {
        for name in ["Grok Bot", "GrokBot", "grok-bot"] {
            let p = appdata.join(name);
            if p.is_dir() {
                out.push(p);
            }
        }
    }
    if let Some(home) = dirs::home_dir() {
        let p = home.join(".grokbot");
        if p.is_dir() {
            out.push(p);
        }
    }
    out
}

fn read_json(path: &Path) -> Result<Value, String> {
    let text =
        std::fs::read_to_string(path).map_err(|e| format!("读取 {} 失败: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("解析 {} JSON 失败: {e}", path.display()))
}

fn maybe_decrypt(master_key: &Option<MasterKey>, raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    // Plaintext JSON object/array — do not treat as ciphertext.
    if (trimmed.starts_with('{') && trimmed.ends_with('}'))
        || (trimmed.starts_with('[') && trimmed.ends_with(']'))
    {
        return trimmed.to_string();
    }
    match master_key {
        Some(mk) => decrypt_secret_value(mk, trimmed).unwrap_or_else(|_| trimmed.to_string()),
        None => trimmed.to_string(),
    }
}

fn extract_string(obj: &Value, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(v) = obj.get(*key).and_then(|x| x.as_str()) {
            let t = v.trim();
            if !t.is_empty() {
                return Some(t.to_string());
            }
        }
    }
    None
}

/// Parse `{active, accounts:[…]}` (or a lone account / array) and decrypt nested tokens.
fn parse_cursor_accounts(
    master_key: &Option<MasterKey>,
    plain: &str,
) -> (Option<String>, Option<String>, Option<String>) {
    let Ok(val) = serde_json::from_str::<Value>(plain) else {
        // Raw JWT pasted as the whole value
        if plain.starts_with("eyJ") {
            return (Some(plain.to_string()), None, None);
        }
        return (None, None, None);
    };

    let active_id = val
        .get("active")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let mut records: Vec<&Value> = Vec::new();
    match &val {
        Value::Array(arr) => records.extend(arr.iter()),
        Value::Object(map) => {
            if let Some(Value::Array(arr)) = map.get("accounts") {
                records.extend(arr.iter());
            } else if map.contains_key("cursor-access-token")
                || map.contains_key("accessToken")
                || map.contains_key("cursor-refresh-token")
            {
                records.push(&val);
            } else if let Some(Value::Object(_)) = map.get("accounts") {
                records.push(map.get("accounts").unwrap());
            }
        }
        _ => {}
    }

    // Prefer the active account when ids match.
    if let Some(ref aid) = active_id {
        if let Some(pos) = records.iter().position(|rec| {
            extract_string(rec, &["id", "accountId", "account_id", "email"])
                .as_deref()
                == Some(aid.as_str())
                || rec
                    .get("id")
                    .and_then(|v| v.as_str())
                    .map(|s| s == aid)
                    .unwrap_or(false)
        }) {
            let preferred = records.remove(pos);
            records.insert(0, preferred);
        }
    }

    let mut access = None;
    let mut refresh = None;
    let mut email = None;

    for rec in records {
        if access.is_none() {
            if let Some(raw) = extract_string(
                rec,
                &[
                    "cursor-access-token",
                    "accessToken",
                    "access_token",
                    "sessionToken",
                    "token",
                ],
            ) {
                let decoded = maybe_decrypt(master_key, &raw);
                if decoded.starts_with("eyJ") || !decoded.is_empty() {
                    access = Some(decoded);
                }
            }
        }
        if refresh.is_none() {
            if let Some(raw) = extract_string(
                rec,
                &["cursor-refresh-token", "refreshToken", "refresh_token"],
            ) {
                let decoded = maybe_decrypt(master_key, &raw);
                if !decoded.is_empty() {
                    refresh = Some(decoded);
                }
            }
        }
        if email.is_none() {
            email = extract_string(rec, &["email", "profileEmail", "userEmail"]);
            if email.is_none() {
                if let Some(profile) = rec.get("profile") {
                    email = extract_string(profile, &["email"]);
                }
            }
        }
        if let Some(tokens) = rec.get("tokens").or_else(|| rec.get("auth")) {
            if access.is_none() {
                if let Some(raw) =
                    extract_string(tokens, &["cursor-access-token", "accessToken", "access_token"])
                {
                    access = Some(maybe_decrypt(master_key, &raw));
                }
            }
            if refresh.is_none() {
                if let Some(raw) =
                    extract_string(tokens, &["cursor-refresh-token", "refreshToken", "refresh_token"])
                {
                    refresh = Some(maybe_decrypt(master_key, &raw));
                }
            }
        }
        if access.is_some() && refresh.is_some() {
            break;
        }
    }

    (access, refresh, email)
}

/// Scan known text/json files for classic `sbi_…` renewal credentials (~47 chars).
fn discover_sbi_renewal(dirs: &[PathBuf]) -> Option<String> {
    // sbi_ + ~43 url-safe chars ≈ 47 total (allow a range)
    let re = regex_lite_sbi();
    let names = [
        "sand-secrets.json",
        "gateway-descriptor.json",
        "Local State",
        "Preferences",
        "config.json",
        "credentials.json",
        ".env",
    ];
    for dir in dirs {
        for name in names {
            let path = dir.join(name);
            if let Some(found) = scan_file_for_sbi(&path, &re) {
                return Some(found);
            }
        }
        // Shallow scan of small text files + persistence blobs (best-effort)
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                let meta = entry.metadata().ok();
                let len = meta.as_ref().map(|m| m.len()).unwrap_or(0);
                if len == 0 || len > 2_000_000 {
                    continue;
                }
                let fname = path
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_lowercase();
                if fname.ends_with(".json")
                    || fname.ends_with(".txt")
                    || fname.ends_with(".env")
                    || fname.ends_with(".blob")
                    || fname.contains("secret")
                    || fname.contains("credential")
                    || fname.contains("gateway")
                {
                    if let Some(found) = scan_file_for_sbi(&path, &re) {
                        return Some(found);
                    }
                }
            }
        }
        let persist = dir.join("sand-client-persistence");
        if persist.is_dir() {
            if let Ok(entries) = std::fs::read_dir(&persist) {
                for entry in entries.flatten().take(64) {
                    let path = entry.path();
                    let len = entry.metadata().map(|m| m.len()).unwrap_or(0);
                    if len > 0 && len < 512_000 {
                        if let Some(found) = scan_file_for_sbi(&path, &re) {
                            return Some(found);
                        }
                    }
                }
            }
        }
    }
    None
}

/// Minimal sbi_ matcher without pulling the `regex` crate (keep deps lean).
fn regex_lite_sbi() -> SbiMatcher {
    SbiMatcher
}

struct SbiMatcher;

impl SbiMatcher {
    fn find<'a>(&self, text: &'a str) -> Option<&'a str> {
        let bytes = text.as_bytes();
        let mut i = 0;
        while i + 4 < bytes.len() {
            if &bytes[i..i + 4] == b"sbi_" {
                let start = i;
                i += 4;
                while i < bytes.len() {
                    let c = bytes[i];
                    if c.is_ascii_alphanumeric() || c == b'_' || c == b'-' {
                        i += 1;
                    } else {
                        break;
                    }
                }
                let candidate = &text[start..i];
                // Typical length ~47; accept a sensible window.
                if candidate.len() >= 20 && candidate.len() <= 80 {
                    return Some(candidate);
                }
            } else {
                i += 1;
            }
        }
        None
    }
}

fn scan_file_for_sbi(path: &Path, matcher: &SbiMatcher) -> Option<String> {
    let data = std::fs::read(path).ok()?;
    // Prefer UTF-8; also try lossy for binary blobs.
    let text = String::from_utf8_lossy(&data);
    matcher.find(&text).map(|s| s.to_string())
}

fn import_from_dir(
    dir: &Path,
    master_key: &mut Option<MasterKey>,
    errors: &mut Vec<String>,
) -> (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
) {
    let local_state = dir.join("Local State");
    let secrets_path = dir.join("sand-secrets.json");

    if master_key.is_none() && local_state.is_file() {
        match load_master_key_from_local_state(&local_state) {
            Ok(k) => *master_key = Some(k),
            Err(e) => errors.push(format!("解包 {} os_crypt 失败: {e}", local_state.display())),
        }
    }

    let mut machine_id = None;
    let mut access = None;
    let mut refresh = None;
    let mut email = None;

    if secrets_path.is_file() {
        match read_json(&secrets_path) {
            Ok(secrets) => {
                if let Some(obj) = secrets.as_object() {
                    for key in [
                        "cursor-machine-id",
                        "machineId",
                        "machine_id",
                        "SAND_MACHINE_ID",
                    ] {
                        if let Some(raw) = obj.get(key).and_then(|v| v.as_str()) {
                            let decoded = maybe_decrypt(master_key, raw);
                            if !decoded.trim().is_empty() {
                                machine_id = Some(decoded.trim().to_string());
                                break;
                            }
                        }
                    }

                    for key in ["cursor-accounts", "accounts", "cursorAccounts"] {
                        if let Some(raw) = obj.get(key).and_then(|v| v.as_str()) {
                            let plain = maybe_decrypt(master_key, raw);
                            let (a, r, e) = parse_cursor_accounts(master_key, &plain);
                            access = a.or(access);
                            refresh = r.or(refresh);
                            email = e.or(email);
                            break;
                        } else if let Some(arr_or_obj) = obj.get(key) {
                            let plain = arr_or_obj.to_string();
                            let (a, r, e) = parse_cursor_accounts(master_key, &plain);
                            access = a.or(access);
                            refresh = r.or(refresh);
                            email = e.or(email);
                            break;
                        }
                    }
                }
            }
            Err(e) => errors.push(e),
        }
    }

    // Secondary: gateway-descriptor.json may list machine id / paths
    let descriptor = dir.join("gateway-descriptor.json");
    if descriptor.is_file() {
        if let Ok(val) = read_json(&descriptor) {
            if machine_id.is_none() {
                if let Some(mid) =
                    extract_string(&val, &["machineId", "machine_id", "cursor-machine-id"])
                {
                    machine_id = Some(maybe_decrypt(master_key, &mid));
                }
            }
        }
    }

    (machine_id, access, refresh, email)
}

pub fn import_from_grok_bot() -> Result<ImportResult, StoreError> {
    let mut errors = Vec::new();
    let dirs = grok_bot_dirs();
    if dirs.is_empty() {
        return Ok(ImportResult {
            machine_id: None,
            has_access_token: false,
            has_refresh_token: false,
            profile_email: None,
            errors: vec![
                "未找到 Grok Bot 数据目录（%APPDATA%\\Grok Bot 或 %USERPROFILE%\\.grokbot）"
                    .into(),
            ],
            inference_renewal_available: false,
            note: "请确认已安装并登录过 Grok Bot；推理续期凭证（sbi_…）仍可手动粘贴。".into(),
        });
    }

    let mut master_key: Option<MasterKey> = None;
    let mut machine_id = None;
    let mut access = None;
    let mut refresh = None;
    let mut email = None;

    for dir in &dirs {
        let (m, a, r, e) = import_from_dir(dir, &mut master_key, &mut errors);
        machine_id = machine_id.or(m);
        access = access.or(a);
        refresh = refresh.or(r);
        email = email.or(e);
    }

    if let Some(ref mid) = machine_id {
        if let Err(e) = secure_store::set_machine_id_secret(mid) {
            errors.push(format!("保存 machine id 失败: {e}"));
        }
    }
    if access.is_some() || refresh.is_some() {
        if let Err(e) = secure_store::set_session_tokens(access.as_deref(), refresh.as_deref()) {
            errors.push(format!("保存会话令牌失败: {e}"));
        }
    }
    if let Some(ref em) = email {
        let mut s = secure_store::load_settings();
        s.profile_email = em.clone();
        let _ = secure_store::save_settings(&s);
    }

    let mut renewal_found = false;
    if let Some(sbi) = discover_sbi_renewal(&dirs) {
        match secure_store::set_renewal_credential(&sbi) {
            Ok(()) => renewal_found = true,
            Err(e) => errors.push(format!("保存发现的续期凭证失败: {e}")),
        }
    }

    let has_access = access.is_some() || secure_store::has_access_token();
    let has_refresh = refresh.is_some() || secure_store::has_refresh_token();

    let note = if renewal_found {
        "已从 Grok Bot 导入会话凭证，并发现续期凭证（sbi_…）。".into()
    } else if has_access || machine_id.is_some() {
        "已从 Grok Bot 导入会话凭证。未在本地文件中发现 sbi_ 续期凭证 — 请手动粘贴 SAND_INFERENCE_RENEWAL_CREDENTIAL（会话 JWT 不能用于推理）。".into()
    } else {
        "导入未获得可用会话凭证；请检查 Grok Bot 是否已登录，或手动粘贴续期凭证。".into()
    };

    Ok(ImportResult {
        machine_id,
        has_access_token: has_access,
        has_refresh_token: has_refresh,
        profile_email: email,
        errors,
        inference_renewal_available: renewal_found || secure_store::has_renewal(),
        note,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sbi_matcher_finds_typical_token() {
        let m = SbiMatcher;
        let sample = "foo sbi_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abc bar";
        let found = m.find(sample).unwrap();
        assert!(found.starts_with("sbi_"));
        assert!(found.len() >= 20);
    }

    #[test]
    fn parse_accounts_nested_plaintext_json() {
        let json = r#"{"active":"a1","accounts":[{"id":"a1","cursor-access-token":"eyJhbGciOiJ.test.sig","cursor-refresh-token":"eyJhbGciOiJ.refresh.sig","profile":{"email":"u@example.com"}}]}"#;
        let (a, r, e) = parse_cursor_accounts(&None, json);
        assert!(a.unwrap().starts_with("eyJ"));
        assert!(r.unwrap().starts_with("eyJ"));
        assert_eq!(e.as_deref(), Some("u@example.com"));
    }
}
