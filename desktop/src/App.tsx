import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type {
  AppSettings,
  CredentialStatus,
  GatewayStatus,
  ImportResult,
  NavId,
} from "./types";
import { APP_VERSION, NAV_ITEMS } from "./types";
import "./App.css";

export default function App() {
  const [nav, setNav] = useState<NavId>("workbench");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [status, setStatus] = useState<GatewayStatus | null>(null);
  const [creds, setCreds] = useState<CredentialStatus | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [renewalInput, setRenewalInput] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [iframeKey, setIframeKey] = useState(0);
  /** Only auto-route to setup on the very first credential load — never on poll. */
  const initialNavApplied = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [g, c, s] = await Promise.all([
        api.gatewayStatus(),
        api.getCredentialStatus(),
        api.getSettings(),
      ]);
      setStatus(g);
      setCreds(c);
      setSettings(s);
      // First launch only: land on 凭证 if onboarding unfinished.
      // Do NOT re-force setup on the 3s poll — that trapped every sidebar click
      // whenever sbi_ / has_renewal was still missing.
      if (!initialNavApplied.current) {
        initialNavApplied.current = true;
        if (!c.onboarding_done) {
          setNav("setup");
        }
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const running = !!status?.running;

  const adminSrc = useMemo(() => {
    if (!running || !status) return null;
    const item = NAV_ITEMS.find((n) => n.id === nav);
    const hash = item?.hash;
    if (nav === "overview" || nav === "setup") return null;
    const base = status.admin_url.replace(/\/$/, "");
    if (hash) return `${base}#${hash}`;
    return base;
  }, [running, status, nav]);

  async function withBusy(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  async function onStart() {
    await withBusy(async () => {
      const g = await api.startGateway();
      setStatus(g);
      setMessage(`网关已启动：${g.base_url}${g.launch_mode ? `（${g.launch_mode}）` : ""}`);
      setIframeKey((k) => k + 1);
      setNav("workbench");
    });
  }

  async function onStop() {
    await withBusy(async () => {
      const g = await api.stopGateway();
      setStatus(g);
      setMessage("网关已停止");
    });
  }

  async function onImport() {
    await withBusy(async () => {
      const result = await api.importGrokBotCredentials();
      setImportResult(result);
      setMessage(result.note);
      if (result.errors.length) {
        setError(result.errors.join("；"));
      }
    });
  }

  async function onSaveRenewal() {
    await withBusy(async () => {
      await api.setRenewalCredential(renewalInput);
      setRenewalInput("");
      setMessage("续期凭证已安全保存（不会回显）");
      // Stay out of the import trap: after successful sbi_ save, go to 工作台.
      setNav("workbench");
    });
  }

  async function onSaveApiKey() {
    await withBusy(async () => {
      await api.setApiKey(apiKeyInput);
      setApiKeyInput("");
      setMessage("本地 API Key 已保存");
    });
  }

  async function onSaveSettings() {
    if (!settings) return;
    await withBusy(async () => {
      const next = await api.saveSettings({
        ...settings,
        onboarding_done: true,
      });
      setSettings(next);
      setMessage("设置已保存");
      setSettingsOpen(false);
    });
  }

  async function finishOnboarding() {
    if (!settings) return;
    await withBusy(async () => {
      const next = await api.saveSettings({ ...settings, onboarding_done: true });
      setSettings(next);
      setNav("workbench");
    });
  }


  const workbenchSection = NAV_ITEMS.find((n) => n.id === nav);
  const emptyTitle =
    nav === "workbench"
      ? "工作台尚未就绪"
      : `${workbenchSection?.label ?? "功能页"}尚未就绪`;
  const emptyBody =
    nav === "workbench"
      ? "启动本地网关后，将在此内嵌加载 http://127.0.0.1:<port>/admin。侧栏「模型 / 密钥 / 审计 / 媒体 / 试用」会深链到对应分区。"
      : `「${workbenchSection?.label ?? nav}」需要本地网关运行后才能打开。可先启动网关，或前往「凭证」检查导入状态。`;

  function selectNav(id: NavId) {
    setNav(id);
    if (id !== "overview" && id !== "setup" && running) {
      setIframeKey((k) => k + 1);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo-dot" />
          <div>
            <div className="brand-title">grokbot2api</div>
            <div className="brand-sub">v{APP_VERSION} · 工作台</div>
          </div>
        </div>
        <nav className="nav">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-item ${nav === item.id ? "active" : ""}`}
              onClick={() => selectNav(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`badge ${running ? "ok" : "off"}`}>
            {running ? `运行中 · ${status?.port}` : "已停止"}
          </span>
          <div className="sidebar-actions">
            <button
              disabled={busy || running}
              onClick={() => void onStart()}
            >
              启动
            </button>
            <button disabled={busy || !running} onClick={() => void onStop()}>
              停止
            </button>
            <button type="button" className="ghost" onClick={() => setSettingsOpen(true)}>
              设置
            </button>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="statusbar">
          <div className="status-left">
            <strong>
              {NAV_ITEMS.find((n) => n.id === nav)?.label ?? "工作台"}
            </strong>
            {status?.base_url && (
              <span className="muted mono">{status.base_url}</span>
            )}
          </div>
          <div className="status-right">
            <span className="muted">
              续期 {creds?.has_renewal ? "✓" : "✗"} · 会话{" "}
              {creds?.has_access_token ? "✓" : "—"} · 机号{" "}
              {creds?.machine_id ? "✓" : "—"}
            </span>
          </div>
        </header>

        {(message || error || status?.last_error || (creds && !creds.has_renewal)) && (
          <div className="banners">
            {creds && !creds.has_renewal && (
              <div className="banner warn">
                缺少推理续期凭证（sbi_…）。浏览各功能页不受影响；网关可以启动，但推理调用会失败。请到
                <button
                  type="button"
                  className="banner-link"
                  onClick={() => selectNav("setup")}
                >
                  凭证
                </button>
                页粘贴保存。会话 JWT 不能替代 sbi_。
              </div>
            )}
            {message && <div className="banner info">{message}</div>}
            {error && <div className="banner err">{error}</div>}
            {status?.last_error && (
              <div className="banner warn">网关：{status.last_error}</div>
            )}
          </div>
        )}

        <main className="main">
          {nav === "overview" ? (
            <div className="overview-panel">
              <h2>总览</h2>
              <div className="cards">
                <section className="card">
                  <h3>网关</h3>
                  <div className="metric">{running ? "运行中" : "已停止"}</div>
                  <ul className="kv">
                    <li>
                      <span>地址</span>
                      <code>{status?.base_url ?? "—"}</code>
                    </li>
                    <li>
                      <span>PID</span>
                      <span>{status?.pid ?? "—"}</span>
                    </li>
                    <li>
                      <span>模式</span>
                      <span>{status?.launch_mode ?? "—"}</span>
                    </li>
                    <li>
                      <span>版本</span>
                      <span>v{APP_VERSION}</span>
                    </li>
                  </ul>
                  <div className="row">
                    <button
                      disabled={busy || running}
                      onClick={() => void onStart()}
                    >
                      启动网关
                    </button>
                    <button disabled={busy || !running} onClick={() => void onStop()}>
                      停止
                    </button>
                    <button
                      disabled={!running}
                      onClick={() => selectNav("workbench")}
                    >
                      打开工作台
                    </button>
                  </div>
                </section>
                <section className="card">
                  <h3>凭证状态</h3>
                  <ul className="kv">
                    <li>
                      <span>推理续期</span>
                      <span className={creds?.has_renewal ? "ok" : "bad"}>
                        {creds?.has_renewal ? "已配置" : "缺失"}
                      </span>
                    </li>
                    <li>
                      <span>会话 access</span>
                      <span>
                        {creds?.has_access_token
                          ? "已导入"
                          : "本机未存储会话 JWT"}
                      </span>
                    </li>
                    <li>
                      <span>会话 refresh</span>
                      <span>
                        {creds?.has_refresh_token
                          ? "已导入"
                          : "本机未存储会话 JWT"}
                      </span>
                    </li>
                    <li>
                      <span>机号</span>
                      <code className="truncate">{creds?.machine_id || "—"}</code>
                    </li>
                    <li>
                      <span>邮箱</span>
                      <span>{creds?.profile_email || "—"}</span>
                    </li>
                  </ul>
                  <p className="muted">{creds?.inference_note}</p>
                  <button onClick={() => selectNav("setup")}>凭证向导</button>
                </section>
              </div>
            </div>
          ) : nav === "setup" ? (
            <div className="setup-panel">
              <h2>凭证向导</h2>
              <p className="lead">
                可从本机 Grok Bot 导入 <strong>机号</strong> 与{" "}
                <strong>会话 JWT</strong>（Dashboard / 图像用量路径）。
                sand-secrets <em>通常不含</em> 推理用的{" "}
                <code>sbi_…</code> 续期凭证；若本地文件中发现会自动写入，否则请手动粘贴。
              </p>

              <section className="card">
                <h3>1. 从 Grok Bot 导入</h3>
                <p>
                  读取 <code>%APPDATA%\Grok Bot\</code>（Local State DPAPI → AES-GCM v10）与{" "}
                  <code>%USERPROFILE%\.grokbot\</code>。明文令牌不会返回到前端。
                </p>
                <button disabled={busy} onClick={() => void onImport()}>
                  导入 Grok Bot 凭证
                </button>
                {importResult && (
                  <div className="import-result">
                    <div className="ok-line">{importResult.note}</div>
                    <div>机号：{importResult.machine_id || "—"}</div>
                    <div>
                      access / refresh：
                      {importResult.has_access_token ? "有" : "本机未存储"} /{" "}
                      {importResult.has_refresh_token ? "有" : "本机未存储"}
                    </div>
                    <div>
                      续期凭证：
                      {importResult.inference_renewal_available
                        ? "已就绪"
                        : "未发现（请粘贴）"}
                    </div>
                    <div>邮箱：{importResult.profile_email || "—"}</div>
                  </div>
                )}
              </section>

              <section className="card">
                <h3>2. 粘贴推理续期凭证（sbi_…）</h3>
                <p className="warn-inline">
                  会话 JWT <em>不能</em> 作为{" "}
                  <code>SAND_INFERENCE_RENEWAL_CREDENTIAL</code>
                  。请粘贴 <code>sbi_</code> 开头的续期凭证。
                </p>
                <textarea
                  id="renewal-credential-input"
                  rows={3}
                  placeholder="粘贴 sbi_… 续期凭证（保存后不会回显）"
                  value={renewalInput}
                  onChange={(e) => setRenewalInput(e.target.value)}
                />
                <div className="row">
                  <button
                    disabled={busy || !renewalInput.trim()}
                    onClick={() => void onSaveRenewal()}
                  >
                    保存续期凭证
                  </button>
                  <span className="muted">
                    当前：{creds?.has_renewal ? "已保存" : "未设置"}
                  </span>
                </div>
              </section>

              <section className="card">
                <h3>3. 可选 · 本地代理 API Key</h3>
                <input
                  type="password"
                  placeholder="GROK_BUILD_PROXY_API_KEY（可留空）"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                />
                <button disabled={busy} onClick={() => void onSaveApiKey()}>
                  保存 API Key
                </button>
              </section>

              <section className="card">
                <button disabled={busy} onClick={() => void finishOnboarding()}>
                  完成并前往工作台
                </button>
              </section>
            </div>
          ) : (
            <div className="workbench">
              {running && adminSrc ? (
                <iframe
                  key={iframeKey}
                  title="admin-workbench"
                  src={adminSrc}
                  className="admin-frame"
                  allow="clipboard-read; clipboard-write"
                />
              ) : (
                <div className="empty-state">
                  <h2>{emptyTitle}</h2>
                  <p>{emptyBody}</p>
                  <div className="empty-actions">
                    <button
                      disabled={busy}
                      onClick={() => void onStart()}
                    >
                      启动网关并打开工作台
                    </button>
                    <button onClick={() => selectNav("setup")}>先完成凭证配置</button>
                  </div>
                  <ul className="hints">
                    <li>
                      续期凭证：{creds?.has_renewal ? "已配置" : "缺失（推理调用需要；启动网关仍可进行）"}
                    </li>
                    <li>机号：{creds?.machine_id || "未导入"}</li>
                    <li>
                      会话令牌：
                      {creds?.has_access_token ? "已导入" : "无"}
                    </li>
                  </ul>
                </div>
              )}
            </div>
          )}
        </main>
      </div>

      {settingsOpen && settings && (
        <div className="drawer-backdrop" onClick={() => setSettingsOpen(false)}>
          <aside
            className="drawer"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-label="设置"
          >
            <h2>设置</h2>
            <label>
              监听地址
              <input
                value={settings.host}
                onChange={(e) =>
                  setSettings({ ...settings, host: e.target.value })
                }
              />
            </label>
            <label>
              端口
              <input
                type="number"
                value={settings.port}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    port: Number(e.target.value) || 8765,
                  })
                }
              />
            </label>
            <label>
              机号（SAND_MACHINE_ID）
              <input
                value={settings.machine_id}
                onChange={(e) =>
                  setSettings({ ...settings, machine_id: e.target.value })
                }
                placeholder="可从 Grok Bot 导入"
              />
            </label>
            <p className="muted">
              网关进程会注入已保存的续期凭证、会话令牌、API Key 与机号。修改端口后请重启网关。
            </p>
            <div className="row">
              <button disabled={busy} onClick={() => void onSaveSettings()}>
                保存
              </button>
              <button className="ghost" onClick={() => setSettingsOpen(false)}>
                关闭
              </button>
            </div>
            <hr />
            <button
              className="ghost danger"
              disabled={busy}
              onClick={() =>
                void withBusy(async () => {
                  await api.clearImportedSession();
                  setMessage("已清除导入的会话令牌");
                })
              }
            >
              清除导入的会话令牌
            </button>
          </aside>
        </div>
      )}
    </div>
  );
}
