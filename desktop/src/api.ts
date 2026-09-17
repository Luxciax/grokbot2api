import { invoke } from "@tauri-apps/api/core";
import type {
  AppSettings,
  CredentialStatus,
  GatewayStatus,
  ImportResult,
} from "./types";

export const api = {
  gatewayStatus: () => invoke<GatewayStatus>("gateway_status"),
  startGateway: () => invoke<GatewayStatus>("start_gateway"),
  stopGateway: () => invoke<GatewayStatus>("stop_gateway"),
  getSettings: () => invoke<AppSettings>("get_settings"),
  saveSettings: (settings: AppSettings) =>
    invoke<AppSettings>("save_settings", { settings }),
  getCredentialStatus: () => invoke<CredentialStatus>("get_credential_status"),
  setRenewalCredential: (value: string) =>
    invoke<CredentialStatus>("set_renewal_credential", { value }),
  setApiKey: (value: string) => invoke<CredentialStatus>("set_api_key", { value }),
  importGrokBotCredentials: () =>
    invoke<ImportResult>("import_grok_bot_credentials"),
  clearImportedSession: () => invoke<CredentialStatus>("clear_imported_session"),
  getAdminUrl: () => invoke<string>("get_admin_url"),
};
