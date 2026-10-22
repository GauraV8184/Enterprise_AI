/** Runtime configuration read from Vite's build-time environment. */

function trimEnv(value: string | undefined): string {
  return (value ?? "").trim();
}

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim() || "/api";
  if (trimmed === "/") {
    return "";
  }
  return trimmed.replace(/\/+$/, "");
}

export const APP_NAME = "Enterprise AI Intelligence Platform";

export const API_BASE_URL = normalizeBaseUrl(
  trimEnv(import.meta.env.VITE_API_BASE_URL),
);

export const SUPABASE_URL = trimEnv(import.meta.env.VITE_SUPABASE_URL);
export const SUPABASE_ANON_KEY = trimEnv(
  import.meta.env.VITE_SUPABASE_ANON_KEY,
);

/**
 * Both Supabase values or neither. A half-configured environment is surfaced
 * instead of silently falling back to the development identity stub.
 */
export const SUPABASE_CONFIGURED = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
export const SUPABASE_MISCONFIGURED =
  Boolean(SUPABASE_URL) !== Boolean(SUPABASE_ANON_KEY);

const configuredMaxUpload = Number(trimEnv(import.meta.env.VITE_MAX_UPLOAD_MB));

export const MAX_UPLOAD_MB =
  Number.isFinite(configuredMaxUpload) && configuredMaxUpload > 0
    ? configuredMaxUpload
    : 25;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

export const DEFAULT_DEV_USER_ID =
  trimEnv(import.meta.env.VITE_DEV_USER_ID) || "dev-user";

/** Extensions accepted by the backend's upload validator. */
export const ACCEPTED_EXTENSIONS = [
  ".pdf",
  ".docx",
  ".xlsx",
  ".csv",
  ".txt",
  ".md",
  ".markdown",
] as const;

export const UPLOAD_ACCEPT = ACCEPTED_EXTENSIONS.join(",");

/** Where the FastAPI Swagger UI lives for the configured API origin. */
export const API_DOCS_URL = API_BASE_URL.startsWith("http")
  ? `${new URL(API_BASE_URL).origin}/docs`
  : "/docs";
