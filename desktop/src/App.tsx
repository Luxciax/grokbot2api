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

/** Flatten Tauri/JS errors so UI never shows [object Object]. */
function formatErr(err: unknown): string {
  if (err == null) return "";
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message || String(err);
  if (typeof err === "object") {
    const o = err as Record<string, unknown>;
    if (typeof o.message === "string" && o.message) return o.message;
    if (o.error != null) return formatErr(o.error);
    try {
      return JSON.stringify(err);
    } catch {
      return Object.prototype.toString.call(err);
    }
  }
  return String(err);
}

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
  const [setupStep, setSetupStep] = useState(1);
  const [renewalNoticeDismissed, setRenewalNoticeDismissed] = useState(false);
  /** Only auto-route to setup on the very first credential load — never on poll. */
  const initialNavApplied = useRef(false);
  const hostInputRef = useRef<HTMLInputElement>(null);
  const prevAdminBase = useRef<string | null>(null);
  const lastGatewayErr = useRef<string | null>(null);

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
      // Surface new gateway last_error once via toast (no sticky banner stack).
      if (g.last_error && g.last_error !== lastGatewayErr.current) {
        lastGatewayErr.current = g.last_error;
        setError(`网关：${g.last_error}`);
      } else if (!g.last_error) {
        lastGatewayErr.current = null;
      }
      if (!initialNavApplied.current) {
        initialNavApplied.current = true;
        if (!c.onboarding_done) {
          setNav("setup");
        }
        if (c.has_renewal) setSetupStep(3);
        else if (c.has_access_token) setSetupStep(2);
        else setSetupStep(1);
      }
    } catch (e) {
      setError(formatErr(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(id);
  }, [refresh]);

  // Success toast auto-clear ~3s
  useEffect(() => {
    if (!message) return;
    const t = window.setTimeout(() => setMessage(null), 3000);
    return () => window.clearTimeout(t);
  }, [message]);

  // Error toast: slightly longer
  useEffect(() => {
    if (!error) return;
    const t = window.setTimeout(() => setError(null), 6000);
    return () => window.clearTimeout(t);
  }, [error]);

  // Esc closes settings; focus host field when opened
  useEffect(() => {
    if (!settingsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSettingsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    const focusTimer = window.setTimeout(() => hostInputRef.current?.focus(), 50);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(focusTimer);
    };
  }, [settingsOpen]);

  const running = !!status?.running;

  const adminBase = useMemo(() => {
    if (!running || !status) return null;
    return status.admin_url.replace(/\/$/, "");
  }, [running, status]);

  // Remount iframe only when admin base URL changes (port/host), not on section nav
  useEffect(() => {
    if (adminBase && prevAdminBase.current && adminBase !== prevAdminBase.current) {
      setIframeKey((k) => k + 1);
    }
    prevAdminBase.current = adminBase;
  }, [adminBase]);

  const adminSrc = useMemo(() => {
    if (!adminBase) return null;
    if (nav === "overview" || nav === "setup") return null;
    const item = NAV_ITEMS.find((n) => n.id === nav);
    const hash = item?.hash;
    if (hash) return `${adminBase}?embed=1#${hash}`;
    return `${adminBase}?embed=1`;
  }, [adminBase, nav]);

  async function withBusy(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      setError(formatErr(e));
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  async function onStart() {
    await withBusy(async () => {
      const g = await api.startGateway();
      setStatus(g);
      setMessage(
        `网关已启动${g.launch_mode ? ` · ${g.launch_mode}` : ""}`,
      );
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
      if (result.has_access_token || result.machine_id) {
        setSetupStep(2);
      }
      if (result.inference_renewal_available) {
        setSetupStep(3);
        setRenewalNoticeDismissed(false);
      }
    });
  }

  async function onSaveRenewal() {
    await withBusy(async () => {
      await api.setRenewalCredential(renewalInput);
      setRenewalInput("");
      setMessage("续期凭证已保存");
      setRenewalNoticeDismissed(false);
      setSetupStep(3);
    });
  }

  async function onSaveSettings() {
    if (!settings) return;
    const portChanged = status?.running && settings.port !== status.port;
    await withBusy(async () => {
      const next = await api.saveSettings({
        ...settings,
        onboarding_done: true,
      });
      setSettings(next);
      setMessage(portChanged ? "已保存 · 改端口需重启网关" : "设置已保存");
      setSettingsOpen(false);
    });
  }

  async function finishOnboarding() {
    if (!settings) return;
    await withBusy(async () => {
      const next = await api.saveSettings({
        ...settings,
        onboarding_done: true,
      });
      setSettings(next);
      setMessage("凭证配置完成");
      setNav("workbench");
    });
  }

  function selectNav(id: NavId) {
    setNav(id);
    // Do NOT bump iframeKey — hash-only src change keeps iframe state
  }

  const gwHostPort =
    status != null
      ? `${status.host}:${status.port}`
      : settings
        ? `${settings.host}:${settings.port}`
        : "—";

  const sessionReady =
    !!creds?.has_access_token ||
    !!importResult?.has_access_token ||
    !!importResult?.machine_id;
  const renewalReady = !!creds?.has_renewal;

  const showRenewalNotice =
    !!creds &&
    !creds.has_renewal &&
    !renewalNoticeDismissed &&
    (nav === "overview" || nav === "setup");

  const toastText = error || message || null;
  const toastKind = error ? "err" : "info";

  const mainNav = NAV_ITEMS.filter((n) => n.id !== "setup");
  const setupNav = NAV_ITEMS.find((n) => n.id === "setup");

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true" />
          <span className="brand-text">grokbot2api</span>
          <span className="brand-ver">v{APP_VERSION}</span>
        </div>

        <nav className="nav">
          {mainNav.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-item ${nav === item.id ? "active" : ""}`}
              onClick={() => selectNav(item.id)}
            >
              <span className="nav-dot" />
              {item.label}
            </button>
          ))}
          {setupNav && (
            <>
              <div className="nav-section">配置</div>
              <button
                type="button"
                className={`nav-item ${nav === setupNav.id ? "active" : ""}`}
                onClick={() => selectNav(setupNav.id)}
              >
                <span className="nav-dot" />
                {setupNav.label}
              </button>
            </>
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="gw-status">
            <span
              className={`status-dot ${busy && !running ? "busy" : running ? "on" : ""}`}
            />
            <div className="gw-meta">
              <div className="gw-label">
                {busy && !running ? "启动中…" : running ? "运行中" : "已停止"}
              </div>
              <div className="gw-url">{gwHostPort}</div>
            </div>
          </div>
          <div className="sidebar-actions">
            {running ? (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy}
                onClick={() => void onStop()}
              >
                停止
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busy}
                onClick={() => void onStart()}
              >
                {busy ? "启动中" : "启动"}
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setSettingsOpen(true)}
            >
              设置
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        {nav === "overview" ? (
          <>
            <div className="page-header">
              <div>
                <div className="page-title">总览</div>
                <div className="page-sub">本机网关与凭证状态</div>
              </div>
            </div>
            <div className="page-body">
              <div className="overview-list">
                <div className="ov-row">
                  <div className="ov-key">网关</div>
                  <div className="ov-val">
                    <div className="ov-title">
                      <span>{running ? "运行中" : "已停止"}</span>
                      <span className={`pill-quiet ${running ? "ok" : ""}`}>
                        {running ? "在线" : "离线"}
                      </span>
                    </div>
                    <div className="ov-detail">{gwHostPort}</div>
                    {status?.pid != null && (
                      <div className="ov-detail plain">
                        PID {status.pid}
                        {status.launch_mode ? ` · ${status.launch_mode}` : ""}
                      </div>
                    )}
                  </div>
                  {running ? (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy}
                      onClick={() => void onStop()}
                    >
                      停止
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy}
                      onClick={() => void onStart()}
                    >
                      启动
                    </button>
                  )}
                </div>
                <div className="ov-row">
                  <div className="ov-key">凭证</div>
                  <div className="ov-val">
                    <div className="ov-title">
                      <span>
                        {renewalReady && sessionReady
                          ? "已配置"
                          : sessionReady
                            ? "部分配置"
                            : "未完成"}
                      </span>
                      <span
                        className={`pill-quiet ${renewalReady ? "ok" : "warn"}`}
                      >
                        {renewalReady ? "齐全" : "缺 sbi_"}
                      </span>
                    </div>
                    <div className="ov-detail plain">
                      {renewalReady && sessionReady
                        ? "会话 + sbi_ 续期"
                        : sessionReady
                          ? "已有会话，建议补齐续期凭证"
                          : "导入会话后粘贴续期凭证"}
                    </div>
                    {(creds?.profile_email || creds?.machine_id) && (
                      <div className="ov-detail plain">
                        {[creds.profile_email, creds.machine_id]
                          .filter(Boolean)
                          .join(" · ")}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => selectNav("setup")}
                  >
                    去配置
                  </button>
                </div>
              </div>

              {showRenewalNotice && (
                <div className="inline-notice">
                  <span>
                    缺少 sbi_ 续期凭证。网关可启动，但推理会失败。会话 JWT 不能替代
                    sbi_。
                  </span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => setRenewalNoticeDismissed(true)}
                  >
                    关闭
                  </button>
                </div>
              )}

              {running && (
                <div style={{ marginTop: 16 }}>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => selectNav("workbench")}
                  >
                    打开工作台 →
                  </button>
                </div>
              )}
            </div>
          </>
        ) : nav === "setup" ? (
          <>
            <div className="page-header">
              <div>
                <div className="page-title">凭证</div>
                <div className="page-sub">接入 Grok Bot 会话与续期</div>
              </div>
            </div>
            <div className="page-body">
              <p className="step-note">
                会话 JWT ≠ sbi_ 续期凭证。导入会话后，再粘贴 sbi_ 才能自动续期。
              </p>

              {showRenewalNotice && (
                <div
                  className="inline-notice"
                  style={{ marginBottom: 16, marginTop: 0 }}
                >
                  <span>尚未保存 sbi_ 续期凭证，推理调用会失败。</span>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => setRenewalNoticeDismissed(true)}
                  >
                    关闭
                  </button>
                </div>
              )}

              <div className="stepper">
                <div
                  className={`step ${setupStep === 1 ? "active" : ""} ${setupStep > 1 || sessionReady ? "done" : ""}`}
                >
                  <div className="step-rail">
                    <div className="step-num">1</div>
                    <div className="step-line" />
                  </div>
                  <div className="step-body">
                    <div className="step-title">导入 Grok Bot 会话</div>
                    <div className="step-hint">
                      读取本机 Grok Bot 机号与会话 JWT（明文不回显）。
                    </div>
                    <div className="step-actions">
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={busy}
                        onClick={() => void onImport()}
                      >
                        导入
                      </button>
                      {sessionReady && (
                        <span className="field-hint">
                          {creds?.machine_id
                            ? "机号已就绪"
                            : importResult?.machine_id
                              ? "已导入"
                              : "已导入会话"}
                        </span>
                      )}
                    </div>
                    {importResult && (
                      <div className="import-result">
                        <div className="ok-line">{importResult.note}</div>
                        <div>机号：{importResult.machine_id || "—"}</div>
                        <div>
                          access / refresh：
                          {importResult.has_access_token
                            ? "有"
                            : "本机未存储"}{" "}
                          /{" "}
                          {importResult.has_refresh_token
                            ? "有"
                            : "本机未存储"}
                        </div>
                        <div>
                          续期：
                          {importResult.inference_renewal_available
                            ? "已就绪"
                            : "未发现"}
                        </div>
                        {importResult.profile_email && (
                          <div>邮箱：{importResult.profile_email}</div>
                        )}
                      </div>
                    )}
                  </div>
                </div>

                <div
                  className={`step ${setupStep === 2 ? "active" : ""} ${setupStep > 2 || renewalReady ? "done" : ""}`}
                >
                  <div className="step-rail">
                    <div className="step-num">2</div>
                    <div className="step-line" />
                  </div>
                  <div className="step-body">
                    <div className="step-title">粘贴 sbi_ 续期凭证</div>
                    <div className="step-hint">用于会话过期后自动续期。</div>
                    <div className="field">
                      <label htmlFor="renewal-credential-input">sbi_ 凭证</label>
                      <input
                        id="renewal-credential-input"
                        type="password"
                        autoComplete="off"
                        placeholder="sbi_…"
                        value={renewalInput}
                        onChange={(e) => setRenewalInput(e.target.value)}
                      />
                    </div>
                    <div className="step-actions">
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={
                          busy || !renewalInput.trim().startsWith("sbi_")
                        }
                        onClick={() => void onSaveRenewal()}
                      >
                        保存
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => {
                          setSetupStep(3);
                          setMessage("已跳过续期凭证");
                        }}
                      >
                        跳过
                      </button>
                      {renewalReady && (
                        <span className="field-hint">已保存</span>
                      )}
                    </div>
                  </div>
                </div>

                <div
                  className={`step ${setupStep === 3 ? "active" : ""} ${creds?.onboarding_done ? "done" : ""}`}
                >
                  <div className="step-rail">
                    <div className="step-num">3</div>
                    <div className="step-line" />
                  </div>
                  <div className="step-body">
                    <div className="step-title">可选：本地代理 API Key</div>
                    <div className="step-hint">
                      给客户端用的密钥，可稍后在「密钥」里管理。
                    </div>
                    <div className="field">
                      <label htmlFor="api-key-input">API Key</label>
                      <input
                        id="api-key-input"
                        type="password"
                        autoComplete="off"
                        placeholder="可留空"
                        value={apiKeyInput}
                        onChange={(e) => setApiKeyInput(e.target.value)}
                      />
                    </div>
                    <div className="step-actions">
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={busy}
                        onClick={() =>
                          void withBusy(async () => {
                            if (!settings) return;
                            const keyVal = apiKeyInput.trim();
                            if (keyVal) {
                              await api.setApiKey(keyVal);
                              setApiKeyInput("");
                            }
                            const next = await api.saveSettings({
                              ...settings,
                              onboarding_done: true,
                            });
                            setSettings(next);
                            setMessage(
                              keyVal
                                ? "API Key 已保存 · 配置完成"
                                : "凭证配置完成",
                            );
                            setNav("workbench");
                          })
                        }
                      >
                        完成
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={busy}
                        onClick={() => void finishOnboarding()}
                      >
                        跳过并完成
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="page-body iframe-bleed">
            {running && adminSrc ? (
              <div className="workbench">
                <iframe
                  key={iframeKey}
                  title="admin-workbench"
                  src={adminSrc}
                  className="admin-frame"
                  allow="clipboard-read; clipboard-write"
                />
              </div>
            ) : (
              <div className="empty">
                <div className="empty-title">网关未运行</div>
                <div className="empty-desc">
                  启动后打开「
                  {NAV_ITEMS.find((n) => n.id === nav)?.label ?? nav}」。
                </div>
                <div className="empty-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busy}
                    onClick={() => void onStart()}
                  >
                    启动网关
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => selectNav("setup")}
                  >
                    配置凭证
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </main>

      <div
        className={`overlay ${settingsOpen ? "open" : ""}`}
        onClick={() => setSettingsOpen(false)}
      />
      <aside
        className={`drawer ${settingsOpen ? "open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settingsTitle"
      >
        <div className="drawer-head">
          <div className="drawer-title" id="settingsTitle">
            设置
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label="关闭"
            onClick={() => setSettingsOpen(false)}
          >
            关闭
          </button>
        </div>
        <div className="drawer-body">
          {settings && (
            <>
              <div className="field">
                <label htmlFor="setHost">监听地址</label>
                <input
                  id="setHost"
                  ref={hostInputRef}
                  type="text"
                  value={settings.host}
                  onChange={(e) =>
                    setSettings({ ...settings, host: e.target.value })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="setPort">端口</label>
                <input
                  id="setPort"
                  type="number"
                  value={settings.port}
                  onChange={(e) =>
                    setSettings({
                      ...settings,
                      port: Number(e.target.value) || 18765,
                    })
                  }
                />
                <div className="field-hint">改端口需重启网关</div>
              </div>
              <div className="field">
                <label htmlFor="setMachine">机号（SAND_MACHINE_ID）</label>
                <input
                  id="setMachine"
                  type="text"
                  className="mono"
                  value={settings.machine_id}
                  placeholder="可从 Grok Bot 导入"
                  onChange={(e) =>
                    setSettings({ ...settings, machine_id: e.target.value })
                  }
                />
                <div className="field-hint">可选，用于多机区分</div>
              </div>
              <div className="drawer-danger">
                <button
                  type="button"
                  className="btn btn-danger btn-sm"
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
              </div>
            </>
          )}
        </div>
        <div className="drawer-foot">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setSettingsOpen(false)}
          >
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !settings}
            onClick={() => void onSaveSettings()}
          >
            保存
          </button>
        </div>
      </aside>

      <div className="toast-host" aria-live="polite">
        {toastText && (
          <div className={`toast ${toastKind === "info" ? "" : toastKind}`}>
            {toastText}
          </div>
        )}
      </div>
    </div>
  );
}
