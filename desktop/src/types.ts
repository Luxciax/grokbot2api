export interface GatewayStatus {
  running: boolean;
  host: string;
  port: number;
  base_url: string;
  admin_url: string;
  pid: number | null;
  last_error: string | null;
  python: string | null;
  gateway_root: string | null;
  launch_mode: string | null;
}

export interface AppSettings {
  host: string;
  port: number;
  machine_id: string;
  onboarding_done: boolean;
  profile_email: string;
}

export interface CredentialStatus {
  has_renewal: boolean;
  has_api_key: boolean;
  has_access_token: boolean;
  has_refresh_token: boolean;
  machine_id: string | null;
  profile_email: string | null;
  onboarding_done: boolean;
  inference_note: string;
}

export interface ImportResult {
  machine_id: string | null;
  has_access_token: boolean;
  has_refresh_token: boolean;
  profile_email: string | null;
  errors: string[];
  inference_renewal_available: boolean;
  note: string;
}

/** Shell sidebar destinations. Admin sections deep-link via /admin#… */
export type NavId =
  | "overview"
  | "workbench"
  | "models"
  | "keys"
  | "audits"
  | "media"
  | "playground"
  | "setup";

export const NAV_ITEMS: { id: NavId; label: string; hash?: string }[] = [
  { id: "overview", label: "总览" },
  { id: "workbench", label: "工作台", hash: "" },
  { id: "models", label: "模型", hash: "models" },
  { id: "keys", label: "密钥", hash: "keys" },
  { id: "audits", label: "审计", hash: "audits" },
  { id: "media", label: "媒体", hash: "media" },
  { id: "playground", label: "试用", hash: "playground" },
  { id: "setup", label: "凭证" },
];

export const APP_VERSION = "0.3.5";
