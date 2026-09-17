# grokbot2api 桌面客户端（Tauri 2）

主桌面客户端：用 **Tauri 2 + React + TypeScript** 壳层控制本地 Python 网关，并在窗口内嵌加载 `http://127.0.0.1:<port>/admin` 工作台（不再依赖外置浏览器）。

> 旧版 Python/Tk/`pystray` 客户端仍保留在仓库 [`windows/`](../windows/)，但已弃用；请优先使用本目录。

## 功能

- 内嵌工作台 WebView（总览 / 模型 / 密钥 / 审计 / 媒体 / 试用 / 设置）
- 启动 / 停止本地 `grokbot2api.py`（默认 `127.0.0.1:8765`）
- 系统托盘：打开工作台、启停、退出（关闭主窗口会隐藏到托盘）
- 凭证向导（中文）：
  - 从 `%APPDATA%\Grok Bot\` 导入 `cursor-machine-id` 与会话 JWT
  - **明确提示**：会话令牌可用于用量查询，**不能**当作 `SAND_INFERENCE_RENEWAL_CREDENTIAL`
  - 粘贴 `SAND_INFERENCE_RENEWAL_CREDENTIAL`（`sbi_…`）；会话 JWT ≠ 推理续期；可选本地代理 API Key
- 机密存 Windows 凭据管理器（非 Windows 开发回退为本地混淆文件）；**从不**把明文令牌返回前端日志

## 依赖

- Node.js 20+
- Rust stable（含 `rustc` / `cargo`）
- Windows 10+ 构建 / 运行目标；系统需有 **Python 3.10+**（`python` 在 PATH）以启动网关
- Linux 开发机可脚手架与跑 Rust 单测，但完整 `tauri dev` 还需 webkit2gtk 等（见 [Tauri 前置条件](https://tauri.app/start/prerequisites/)）

## 开发

```powershell
cd desktop
npm install
npm run sync-gateway          # 复制网关 .py 到 src-tauri/resources/gateway
npm run tauri dev             # 或 npm run tauri:dev
```

环境变量（可选）：

| 变量 | 含义 |
| --- | --- |
| `GROKBOT2API_GATEWAY_ROOT` | 强制指定含 `grokbot2api.py` 的目录 |
| `SAND_INFERENCE_RENEWAL_CREDENTIAL` | 覆盖已保存的续期凭证 |
| `GROK_BUILD_PROXY_API_KEY` | 覆盖本地 API Key |

## 打包

```powershell
cd desktop
npm run tauri:build
```

产物位于 `src-tauri/target/release/bundle/`（NSIS / MSI）。CI 另打 portable zip。

## 凭证能力与限制

| 项目 | 能否自动导入 | 用途 |
| --- | --- | --- |
| `cursor-machine-id` | 能（os_crypt） | `SAND_MACHINE_ID` / checksum |
| `cursor-accounts` access/refresh JWT | 能 | Dashboard 用量等；**推理 401** |
| `SAND_INFERENCE_RENEWAL_CREDENTIAL` / grokBotAccessToken | **不能**从会话 JWT 派生 | InferenceService/Stream |

手动测试（Windows）：

1. 安装并登录 Grok Bot 至少一次。
2. 打开本客户端 → 凭证向导 →「导入 Grok Bot 凭证」。
3. 确认机号显示；会话 token 状态为「有」。
4. 粘贴真实续期凭证 → 启动网关 → 工作台应内嵌出现。
5. 用 `/v1/models` 或工作台「试用」验证推理（无续期凭证时应失败）。

Rust 单测（AES-GCM v10，无需 Windows）：

```bash
cd desktop/crates/os_crypt
cargo test -- --nocapture
```

## 架构摘要

- `src/` — React 壳：侧栏（总览 / 工作台 / 设置）、`<iframe>` 工作台
- `crates/os_crypt (path dep used by src-tauri)` — Chromium DPAPI + AES-GCM v10
- `src-tauri/src/grok_import.rs` — Grok Bot 导入
- `src-tauri/src/secure_store.rs` — 凭据管理器 / 设置
- `src-tauri/src/gateway.rs` — 定位 Python + 网关根目录并 spawn
- `scripts/sync-gateway.mjs` — 打包前复制网关源码

## 版本

与仓库网关对齐：`0.3.0`。
