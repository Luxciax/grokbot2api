//! Spawn / stop the local grokbot2api gateway.
//!
//! Preference order:
//!   1. Tauri sidecar / externalBin `grokbot2api-server` (PyInstaller, portable)
//!   2. Bundled Python sources + system `python` (dev / fallback)

use crate::secure_store::{self, AppSettings};
use parking_lot::Mutex;
use serde::Serialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

const GATEWAY_MODULES: &[&str] = &[
    "grokbot2api.py",
    "sand_inference.py",
    "api_common.py",
    "messages_api.py",
    "responses_api.py",
    "model_catalogue.py",
    "image_gen.py",
];

const SIDECAR_NAMES: &[&str] = &[
    "grokbot2api-server",
    "grokbot2api-server.exe",
];

#[derive(Debug, Clone, Serialize)]
pub struct GatewayStatus {
    pub running: bool,
    pub host: String,
    pub port: u16,
    pub base_url: String,
    pub admin_url: String,
    pub pid: Option<u32>,
    pub last_error: Option<String>,
    pub python: Option<String>,
    pub gateway_root: Option<String>,
    pub launch_mode: Option<String>,
}

pub struct GatewayManager {
    inner: Mutex<GatewayInner>,
}

struct GatewayInner {
    child: Option<Child>,
    adopted: bool,
    last_error: Option<String>,
    python: Option<PathBuf>,
    gateway_root: Option<PathBuf>,
    launch_mode: Option<String>,
    log_path: Option<PathBuf>,
}

impl GatewayManager {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(GatewayInner {
                child: None,
                adopted: false,
                last_error: None,
                python: None,
                gateway_root: None,
                launch_mode: None,
                log_path: None,
            }),
        })
    }

    pub fn status(&self) -> GatewayStatus {
        let settings = secure_store::load_settings();
        let mut g = self.inner.lock();
        self.reap_locked(&mut g);
        if g.adopted && g.child.is_none() {
            let host = normalize_listen_host(&settings.host);
            if !port_serves_our_gateway(&host, settings.port) {
                g.adopted = false;
                g.launch_mode = None;
            }
        }
        let running = g.child.is_some() || g.adopted;
        let pid = g.child.as_ref().map(|c| c.id());
        GatewayStatus {
            running,
            host: settings.host.clone(),
            port: settings.port,
            base_url: format!("http://{}:{}", settings.host, settings.port),
            admin_url: format!("http://{}:{}/admin", settings.host, settings.port),
            pid,
            last_error: g.last_error.clone(),
            python: g.python.as_ref().map(|p| p.display().to_string()),
            gateway_root: g.gateway_root.as_ref().map(|p| p.display().to_string()),
            launch_mode: g.launch_mode.clone(),
        }
    }

    pub fn start(&self, resource_dir: Option<PathBuf>) -> Result<GatewayStatus, String> {
        let mut settings = secure_store::load_settings();
        let missing_renewal = !secure_store::has_renewal();
        let renewal_warn = missing_renewal.then_some(
            "缺少推理续期凭证，网关已启动但推理调用会失败".to_string(),
        );


        let mut g = self.inner.lock();
        self.reap_locked(&mut g);
        if g.child.is_some() || g.adopted {
            drop(g);
            return Ok(self.status());
        }

        let data = secure_store::local_data_dir().map_err(|e| e.to_string())?;
        let media = data.join("media");
        let _ = std::fs::create_dir_all(&media);
        let admin_config = data.join("admin_config.json");
        let cache = data.join("token-cache.json");
        let log_path = data.join("gateway.log");

        let listen_host = normalize_listen_host(&settings.host);
        let preferred = if settings.port == 0 { 18765 } else { settings.port };

        // Already our gateway on preferred port → adopt (no second bind).
        if port_serves_our_gateway(&listen_host, preferred) {
            g.adopted = true;
            g.launch_mode = Some("adopted".into());
            g.last_error = Some(
                "端口上已有 grokbot2api（指纹匹配）。工作台将直连该实例。".into(),
            );
            drop(g);
            return Ok(self.status());
        }

        let chosen = pick_listen_port(&listen_host, preferred).ok_or_else(|| {
            format!(
                "端口 {preferred}–{} 均被其他程序占用（非 grokbot2api，常见 Unknown endpoint）。请关闭占用进程或在设置中更换端口。",
                preferred.saturating_add(20)
            )
        })?;
        let mut port_note: Option<String> = None;
        if chosen != settings.port {
            port_note = Some(format!(
                "原端口 {} 被其他程序占用，已自动改用 {chosen}",
                settings.port
            ));
            settings.port = chosen;
            let _ = secure_store::save_settings(&settings);
        }

        let log_file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("无法打开网关日志 {}: {e}", log_path.display()))?;
        let log_err = log_file
            .try_clone()
            .map_err(|e| format!("无法复制网关日志句柄: {e}"))?;

        let mut env: HashMap<String, String> = std::env::vars().collect();
        secure_store::apply_secrets_to_env(&mut env);
        env.insert("PYTHONUTF8".into(), "1".into());
        env.insert("PYTHONIOENCODING".into(), "utf-8".into());
        env.insert("SAND_TOKEN_CACHE".into(), cache.display().to_string());

        // --- Prefer sidecar (portable, no system Python) ---
        if let Some(sidecar) = find_sidecar(resource_dir.as_deref()) {
            let mut cmd = Command::new(&sidecar);
            cmd.arg("--listen")
                .arg(&settings.host)
                .arg("--port")
                .arg(settings.port.to_string())
                .arg("--admin-config")
                .arg(&admin_config)
                .arg("--cache")
                .arg(&cache)
                .current_dir(sidecar.parent().unwrap_or_else(|| Path::new(".")))
                .envs(&env)
                .stdin(Stdio::null())
                .stdout(Stdio::from(log_file))
                .stderr(Stdio::from(log_err));
            apply_no_window(&mut cmd);
            match cmd.spawn() {
                Ok(child) => {
                    g.python = Some(sidecar.clone());
                    g.gateway_root = sidecar.parent().map(|p| p.to_path_buf());
                    g.launch_mode = Some("sidecar".into());
                    g.log_path = Some(log_path.clone());
                    g.adopted = false;
                    g.last_error = None;
                    g.child = Some(child);
                }
                Err(e) => {
                    g.last_error = Some(e.to_string());
                    return Err(format!("启动 sidecar 失败: {e}"));
                }
            }
        } else {
            // --- Fallback: system Python + bundled / repo sources ---
            let python = find_python().ok_or_else(|| {
                "未找到 grokbot2api-server sidecar，也未找到系统 Python。请安装 Python 3.10+ 或使用完整发布包。"
                    .to_string()
            })?;
            let root = resolve_gateway_root(resource_dir.as_deref()).ok_or_else(|| {
                "未找到网关脚本（grokbot2api.py）。请确认 resources/gateway 已打包。"
                    .to_string()
            })?;
            let script = root.join("grokbot2api.py");
            if !script.is_file() {
                return Err(format!("网关入口不存在: {}", script.display()));
            }

            let mut cmd = Command::new(&python);
            cmd.arg(&script)
                .arg("--listen")
                .arg(&settings.host)
                .arg("--port")
                .arg(settings.port.to_string())
                .arg("--admin-config")
                .arg(&admin_config)
                .arg("--cache")
                .arg(&cache)
                .arg("--upstream-script")
                .arg(root.join("sand_inference.py"))
                .current_dir(&root)
                .envs(&env)
                .stdin(Stdio::null())
                .stdout(Stdio::from(log_file))
                .stderr(Stdio::from(log_err));
            apply_no_window(&mut cmd);

            match cmd.spawn() {
                Ok(child) => {
                    g.python = Some(python);
                    g.gateway_root = Some(root);
                    g.launch_mode = Some("python".into());
                    g.log_path = Some(log_path.clone());
                    g.adopted = false;
                    g.last_error = None;
                    g.child = Some(child);
                }
                Err(e) => {
                    g.last_error = Some(e.to_string());
                    return Err(format!("启动网关失败: {e}"));
                }
            }
        }

        let url = format!("http://{}:{}/health", listen_host, settings.port);
        let deadline = Instant::now() + Duration::from_secs(15);
        let mut ready = false;
        while Instant::now() < deadline {
            self.reap_locked(&mut g);
            if g.child.is_none() {
                let tail = read_log_tail(g.log_path.as_deref(), 1200);
                let err = g
                    .last_error
                    .clone()
                    .unwrap_or_else(|| "网关进程已退出".into());
                let msg = if tail.is_empty() {
                    err
                } else {
                    format!("{err}\n---- gateway.log ----\n{tail}")
                };
                g.last_error = Some(msg.clone());
                return Err(msg);
            }
            if http_ok(&url) {
                ready = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        if !ready {
            let tail = read_log_tail(g.log_path.as_deref(), 1200);
            let _ = g.child.take().map(|mut c| {
                let _ = c.kill();
                let _ = c.wait();
            });
            let mut msg = format!(
                "网关未能通过健康检查（/health 需返回 grokbot2api 指纹，含 ok+version）。日志: {}",
                log_path.display()
            );
            if !tail.is_empty() {
                msg.push_str("\n---- gateway.log ----\n");
                msg.push_str(&tail);
            }
            g.last_error = Some(msg.clone());
            return Err(msg);
        }

        let mut notes: Vec<String> = Vec::new();
        if let Some(n) = port_note {
            notes.push(n);
        }
        if let Some(w) = renewal_warn {
            notes.push(w);
        }
        g.last_error = if notes.is_empty() {
            None
        } else {
            Some(notes.join("；"))
        };

        drop(g);
        Ok(self.status())
    }

    pub fn stop(&self) -> Result<GatewayStatus, String> {
        let mut g = self.inner.lock();
        if let Some(mut child) = g.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        g.adopted = false;
        g.last_error = None;
        g.launch_mode = None;
        drop(g);
        Ok(self.status())
    }

    fn reap_locked(&self, g: &mut GatewayInner) {
        if let Some(child) = g.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let tail = read_log_tail(g.log_path.as_deref(), 800);
                    let mut msg = format!("网关已退出: {status}");
                    if !tail.is_empty() {
                        msg.push_str("
---- gateway.log ----
");
                        msg.push_str(&tail);
                    }
                    g.last_error = Some(msg);
                    g.child = None;
                }
                Ok(None) => {}
                Err(e) => {
                    g.last_error = Some(e.to_string());
                    g.child = None;
                }
            }
        }
    }
}

fn apply_no_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let _ = cmd;
}


fn normalize_listen_host(host: &str) -> String {
    let h = host.trim();
    if h.is_empty() || h == "0.0.0.0" || h == "::" || h == "*" {
        "127.0.0.1".into()
    } else {
        h.to_string()
    }
}

fn port_serves_our_gateway(host: &str, port: u16) -> bool {
    http_ok(&format!("http://{host}:{port}/health"))
}

fn port_accepts_tcp(host: &str, port: u16) -> bool {
    use std::net::{TcpStream, ToSocketAddrs};
    let Ok(mut iter) = (host, port).to_socket_addrs() else {
        return false;
    };
    let Some(addr) = iter.next() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok()
}

/// Prefer `start`, then start+1 .. start+20. Skip foreign responders. None if all busy.
fn pick_listen_port(host: &str, start: u16) -> Option<u16> {
    for port in start..=start.saturating_add(20) {
        if port == 0 {
            continue;
        }
        if port_serves_our_gateway(host, port) {
            // Caller handles adopt; treating as usable for bind would fail.
            continue;
        }
        if !port_accepts_tcp(host, port) {
            return Some(port);
        }
        // TCP accepts but not our fingerprint → foreign, skip.
    }
    None
}

fn http_ok(url: &str) -> bool {
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    let Ok(parsed) = url::Url::parse(url) else {
        return false;
    };
    let host = parsed.host_str().unwrap_or("127.0.0.1");
    let port = parsed.port_or_known_default().unwrap_or(80);
    let path = if parsed.path().is_empty() {
        "/"
    } else {
        parsed.path()
    };
    let addr = match (host, port).to_socket_addrs() {
        Ok(mut iter) => iter.next(),
        Err(_) => None,
    };
    let Some(addr) = addr else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(600)));
    let req = format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let mut tmp = [0u8; 1024];
    loop {
        match stream.read(&mut tmp) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&tmp[..n]);
                if buf.len() > 8192 {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    if buf.is_empty() {
        return false;
    }
    let resp = String::from_utf8_lossy(&buf);
    let status_ok = resp.contains(" 200 ")
        || resp.starts_with("HTTP/1.1 200")
        || resp.starts_with("HTTP/1.0 200");
    if !status_ok {
        return false;
    }
    let body_start = resp
        .find("\r\n\r\n")
        .map(|i| i + 4)
        .or_else(|| resp.find("\n\n").map(|i| i + 2))
        .unwrap_or(resp.len());
    let body = &resp[body_start.min(resp.len())..];
    is_our_health_body(body)
}

fn is_our_health_body(body: &str) -> bool {
    let lower = body.to_ascii_lowercase();
    if lower.contains("unknown endpoint") {
        return false;
    }
    if body.contains("\"service\"") && body.contains("grokbot2api") && body.contains("\"ok\"") {
        return true;
    }
    let has_ok = body.contains("\"ok\"") && body.contains("true");
    let has_version = body.contains("\"version\"");
    let has_admin = body.contains("\"admin\"") && body.contains("/admin");
    has_ok && has_version && has_admin
}

fn read_log_tail(path: Option<&Path>, max_bytes: usize) -> String {
    let Some(path) = path else {
        return String::new();
    };
    let Ok(data) = std::fs::read(path) else {
        return String::new();
    };
    let slice = if data.len() > max_bytes {
        &data[data.len() - max_bytes..]
    } else {
        &data[..]
    };
    String::from_utf8_lossy(slice).trim().to_string()
}

fn find_python() -> Option<PathBuf> {
    for name in ["python", "python3"] {
        if let Ok(path) = which::which(name) {
            return Some(path);
        }
    }
    which::which("py").ok()
}

fn find_sidecar(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for name in SIDECAR_NAMES {
                candidates.push(dir.join(name));
                candidates.push(dir.join("binaries").join(name));
                candidates.push(dir.join("sidecar").join(name));
            }
            // Tauri renames externalBin with target triple suffix
            #[cfg(windows)]
            {
                candidates.push(dir.join("grokbot2api-server-x86_64-pc-windows-msvc.exe"));
                candidates.push(dir.join("binaries").join("grokbot2api-server-x86_64-pc-windows-msvc.exe"));
            }
        }
    }

    if let Some(res) = resource_dir {
        for name in SIDECAR_NAMES {
            candidates.push(res.join(name));
            candidates.push(res.join("binaries").join(name));
        }
        #[cfg(windows)]
        {
            candidates.push(res.join("grokbot2api-server-x86_64-pc-windows-msvc.exe"));
        }
    }

    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let bin = PathBuf::from(manifest).join("binaries");
        for name in SIDECAR_NAMES {
            candidates.push(bin.join(name));
        }
        #[cfg(windows)]
        {
            candidates.push(bin.join("grokbot2api-server-x86_64-pc-windows-msvc.exe"));
        }
    }

    if let Ok(p) = std::env::var("GROKBOT2API_SIDECAR") {
        candidates.insert(0, PathBuf::from(p));
    }

    candidates.into_iter().find(|p| p.is_file())
}

fn resolve_gateway_root(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(res) = resource_dir {
        candidates.push(res.join("gateway"));
        candidates.push(res.join("resources").join("gateway"));
    }

    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let tauri_dir = PathBuf::from(manifest);
        candidates.push(tauri_dir.join("resources").join("gateway"));
        candidates.push(tauri_dir.join("..").join(".."));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("gateway"));
            candidates.push(dir.join("resources").join("gateway"));
            candidates.push(dir.join("..").join("gateway"));
            candidates.push(dir.join("..").join("..").join("..").join(".."));
        }
    }

    if let Ok(p) = std::env::var("GROKBOT2API_GATEWAY_ROOT") {
        candidates.insert(0, PathBuf::from(p));
    }

    for c in candidates {
        let Ok(canon) = c.canonicalize() else {
            if looks_like_gateway(&c) {
                return Some(c);
            }
            continue;
        };
        if looks_like_gateway(&canon) {
            return Some(canon);
        }
    }
    None
}

fn looks_like_gateway(dir: &Path) -> bool {
    dir.join("grokbot2api.py").is_file() && dir.join("sand_inference.py").is_file()
}

pub fn admin_url_for(settings: &AppSettings) -> String {
    format!("http://{}:{}/admin", settings.host, settings.port)
}

#[allow(dead_code)]
pub fn gateway_modules() -> &'static [&'static str] {
    GATEWAY_MODULES
}

#[cfg(test)]
mod tests {
    use super::is_our_health_body;

    #[test]
    fn rejects_unknown_endpoint() {
        assert!(!is_our_health_body(r#"{"error":"Unknown endpoint"}"#));
    }

    #[test]
    fn accepts_service_fingerprint() {
        assert!(is_our_health_body(
            r#"{"ok":true,"service":"grokbot2api","version":"0.3.3","admin":"/admin"}"#
        ));
    }

    #[test]
    fn accepts_legacy_ok_version_admin() {
        assert!(is_our_health_body(
            r#"{"ok":true,"version":"0.3.0","admin":"/admin"}"#
        ));
    }
}
