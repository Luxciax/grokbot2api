mod os_crypt {
    pub use ::os_crypt::*;
}

mod gateway;
mod grok_import;
mod secure_store;

use gateway::GatewayManager;
use secure_store::AppSettings;
use serde::Serialize;
use std::sync::Arc;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, State, WindowEvent,
};
use tauri_plugin_autostart::ManagerExt;

struct AppState {
    gateway: Arc<GatewayManager>,
}

#[derive(Serialize)]
struct CredentialStatus {
    has_renewal: bool,
    has_api_key: bool,
    has_access_token: bool,
    has_refresh_token: bool,
    machine_id: Option<String>,
    profile_email: Option<String>,
    onboarding_done: bool,
    /// Session JWT ≠ inference renewal — always reinforce in UI.
    inference_note: String,
}

#[tauri::command]
fn import_grok_bot_credentials() -> Result<grok_import::ImportResult, String> {
    grok_import::import_from_grok_bot().map_err(|e| e.to_string())
}

#[tauri::command]
fn start_gateway(app: AppHandle, state: State<'_, AppState>) -> Result<gateway::GatewayStatus, String> {
    let resource_dir = app.path().resource_dir().ok();
    state.gateway.start(resource_dir)
}

#[tauri::command]
fn stop_gateway(state: State<'_, AppState>) -> Result<gateway::GatewayStatus, String> {
    state.gateway.stop()
}

#[tauri::command]
fn gateway_status(state: State<'_, AppState>) -> gateway::GatewayStatus {
    state.gateway.status()
}

#[tauri::command]
fn get_settings() -> AppSettings {
    secure_store::load_settings()
}

/// Sync OS login autostart with the saved preference.
fn apply_autostart(app: &AppHandle, want: bool) -> Result<(), String> {
    let mgr = app.autolaunch();
    let enabled = mgr.is_enabled().unwrap_or(false);
    if want && !enabled {
        mgr.enable().map_err(|e| e.to_string())?;
    } else if !want && enabled {
        mgr.disable().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn save_settings(app: AppHandle, mut settings: AppSettings) -> Result<AppSettings, String> {
    settings.sanitize_mut();
    // Persist machine id into secret store when provided
    if !settings.machine_id.trim().is_empty() {
        secure_store::set_machine_id_secret(&settings.machine_id).map_err(|e| e.to_string())?;
    }
    secure_store::save_settings(&settings).map_err(|e| e.to_string())?;
    // Apply login autostart after persist so settings.json wins even if registry fails.
    if let Err(e) = apply_autostart(&app, settings.autostart) {
        // Still return saved settings; surface registry error to UI.
        return Err(format!("设置已保存，但开机自启动同步失败：{e}"));
    }
    Ok(secure_store::load_settings())
}

#[tauri::command]
fn get_credential_status() -> CredentialStatus {
    let s = secure_store::load_settings();
    CredentialStatus {
        has_renewal: secure_store::has_renewal(),
        has_api_key: secure_store::get_api_key().is_some(),
        has_access_token: secure_store::has_access_token(),
        has_refresh_token: secure_store::has_refresh_token(),
        machine_id: secure_store::get_machine_id(),
        profile_email: if s.profile_email.is_empty() {
            None
        } else {
            Some(s.profile_email)
        },
        onboarding_done: s.onboarding_done,
        inference_note: "会话令牌可用于用量查询，但不能作为推理续期凭证；请粘贴 SAND_INFERENCE_RENEWAL_CREDENTIAL（通常以 sbi_ 开头）。"
            .into(),
    }
}

#[tauri::command]
fn set_renewal_credential(value: String) -> Result<CredentialStatus, String> {
    secure_store::set_renewal_credential(&value).map_err(|e| e.to_string())?;
    Ok(get_credential_status())
}

#[tauri::command]
fn set_api_key(value: String) -> Result<CredentialStatus, String> {
    secure_store::set_api_key(&value).map_err(|e| e.to_string())?;
    Ok(get_credential_status())
}

#[tauri::command]
fn clear_imported_session() -> Result<CredentialStatus, String> {
    secure_store::clear_session_tokens().map_err(|e| e.to_string())?;
    Ok(get_credential_status())
}

#[tauri::command]
fn get_admin_url() -> String {
    let s = secure_store::load_settings();
    gateway::admin_url_for(&s)
}

fn show_main(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "打开工作台", true, None::<&str>)?;
    let start_i = MenuItem::with_id(app, "start", "启动网关", true, None::<&str>)?;
    let stop_i = MenuItem::with_id(app, "stop", "停止网关", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &start_i, &stop_i, &quit_i])?;

    let gateway = app.state::<AppState>().gateway.clone();
    let gateway_for_menu = gateway.clone();

    let _tray = TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .menu(&menu)
        .tooltip("grokbot2api")
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "start" => {
                let resource_dir = app.path().resource_dir().ok();
                let _ = gateway_for_menu.start(resource_dir);
                show_main(app);
            }
            "stop" => {
                let _ = gateway_for_menu.stop();
            }
            "quit" => {
                let _ = gateway.stop();
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let gateway = GatewayManager::new();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None::<Vec<&'static str>>,
        ))
        .manage(AppState {
            gateway: gateway.clone(),
        })
        .invoke_handler(tauri::generate_handler![
            import_grok_bot_credentials,
            start_gateway,
            stop_gateway,
            gateway_status,
            get_settings,
            save_settings,
            get_credential_status,
            set_renewal_credential,
            set_api_key,
            clear_imported_session,
            get_admin_url,
        ])
        .setup(|app| {
            setup_tray(app.handle())?;

            let settings = secure_store::load_settings();

            // Keep OS autostart in sync with saved preference (covers first run / manual registry edits).
            let _ = apply_autostart(app.handle(), settings.autostart);

            if settings.start_minimized {
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.hide();
                }
            }

            if settings.start_gateway_on_launch {
                let resource_dir = app.path().resource_dir().ok();
                let gw = app.state::<AppState>().gateway.clone();
                let _ = gw.start(resource_dir);
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let settings = secure_store::load_settings();
                if settings.close_to_tray {
                    api.prevent_close();
                    let _ = window.hide();
                } else {
                    // Quit for real: stop gateway, then exit (tray would otherwise keep process alive).
                    api.prevent_close();
                    let app = window.app_handle().clone();
                    let state = app.state::<AppState>();
                    let _ = state.gateway.stop();
                    app.exit(0);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building grokbot2api desktop")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                // Allow exit from tray Quit
                let _ = api;
                let _ = app_handle;
            }
        });
}
