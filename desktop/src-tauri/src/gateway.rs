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
    last_error: Option<String>,
    python: Option<PathBuf>,
    gateway_root: Option<PathBuf>,
    launch_mode: Option<String>,
}

impl GatewayManager {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(GatewayInner {
                child: None,
                last_error: None,
                python: None,
                gateway_root: None,
                launch_mode: None,
            }),
        })
    }

    pub fn status(&self) -> GatewayStatus {
        let settings = secure_store::load_settings();
        let mut g = self.inner.lock();
        self.reap_locked(&mut g);
        let running = g.child.is_some();
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
        let settings = secure_store::load_settings();
        let missing_renewal = !secure_store::has_renewal();
        let renewal_warn = missing_renewal.then_some(
            "缺少推理续期凭证，网关已启动但推理调用会失败".to_string(),
        );


        let mut g = self.inner.lock();
        self.reap_locked(&mut g);
        if g.child.is_some() {
            drop(g);
            return Ok(self.status());
        }

        let data = secure_store::local_data_dir().map_err(|e| e.to_string())?;
        let media = data.join("media");
        let _ = std::fs::create_dir_all(&media);
        let admin_config = data.join("admin_config.json");
        let cache = data.join("token-cache.json");

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
                .stdout(Stdio::null())
                .stderr(Stdio::piped());
            apply_no_window(&mut cmd);
            match cmd.spawn() {
                Ok(child) => {
                    g.python = Some(sidecar.clone());
                    g.gateway_root = sidecar.parent().map(|p| p.to_path_buf());
                    g.launch_mode = Some("sidecar".into());
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
                .stdout(Stdio::null())
                .stderr(Stdio::piped());
            apply_no_window(&mut cmd);

            match cmd.spawn() {
                Ok(child) => {
                    g.python = Some(python);
                    g.gateway_root = Some(root);
                    g.launch_mode = Some("python".into());
                    g.last_error = None;
                    g.child = Some(child);
                }
                Err(e) => {
                    g.last_error = Some(e.to_string());
                    return Err(format!("启动网关失败: {e}"));
                }
            }
        }

        let url = format!("http://{}:{}/health", settings.host, settings.port);
        let deadline = Instant::now() + Duration::from_secs(12);
        let mut ready = false;
        while Instant::now() < deadline {
            self.reap_locked(&mut g);
            if g.child.is_none() {
                let err = g
                    .last_error
                    .clone()
                    .unwrap_or_else(|| "网关进程已退出".into());
                return Err(err);
            }
            if http_ok(&url) {
                ready = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        if !ready {
            g.last_error = Some("网关已启动但健康检查超时（仍可能稍后可用）".into());
        }
        if g.last_error.is_none() {
            if let Some(w) = renewal_warn.clone() {
                g.last_error = Some(w);
            }
        }

        drop(g);
        Ok(self.status())
    }

    pub fn stop(&self) -> Result<GatewayStatus, String> {
        let mut g = self.inner.lock();
        if let Some(mut child) = g.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        g.last_error = None;
        drop(g);
        Ok(self.status())
    }

    fn reap_locked(&self, g: &mut GatewayInner) {
        if let Some(child) = g.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    g.last_error = Some(format!("网关已退出: {status}"));
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
    let _ = stream.set_read_timeout(Some(Duration::from_millis(600)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(600)));
    let req = format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 128];
    let Ok(n) = stream.read(&mut buf) else {
        return false;
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.contains(" 200 ")
        || head.starts_with("HTTP/1.1 200")
        || head.starts_with("HTTP/1.0 200")
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
