/** Human-facing formatting helpers shared across pages. */

const DATE_WITH_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const NUMBER = new Intl.NumberFormat(undefined);

/**
 * Normalise a server timestamp.
 *
 * Supabase emits ISO strings with a timezone. SQLite's datetime('now') emits
 * "YYYY-MM-DD HH:MM:SS" in UTC without a suffix, which Date would otherwise
 * interpret as local time. Adding the suffix is what makes both backends agree.
 */
export function parseServerDate(value: string | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  const candidate = value.includes("T") ? value : value.replace(" ", "T");
  const withZone = /(?:Z|[+-]\d\d:?\d\d)$/i.test(candidate)
    ? candidate
    : `${candidate}Z`;
  const date = new Date(withZone);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseServerDate(value);
  return date ? DATE_WITH_TIME.format(date) : "—";
}

export function formatRelativeTime(value: string | null | undefined): string {
  const date = parseServerDate(value);
  if (!date) {
    return "—";
  }
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);

  if (absolute < 45) {
    return "just now";
  }
  if (absolute < 60 * 60) {
    return RELATIVE.format(Math.round(seconds / 60), "minute");
  }
  if (absolute < 60 * 60 * 24) {
    return RELATIVE.format(Math.round(seconds / 3600), "hour");
  }
  if (absolute < 60 * 60 * 24 * 7) {
    return RELATIVE.format(Math.round(seconds / 86400), "day");
  }
  return formatDateTime(value);
}

export function formatNumber(value: number): string {
  return NUMBER.format(value);
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB"];
  let size = bytes / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unit]}`;
}

export function formatScore(score: number): string {
  if (!Number.isFinite(score)) {
    return "—";
  }
  return score.toFixed(3);
}

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return formatNumber(value);
}

export function humanizeIntent(intent: string): string {
  return intent
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function humanizeRoute(route: string): string {
  const labels: Record<string, string> = {
    chat: "Conversation",
    documents: "Documents",
    research: "Web research",
    analytics: "Analytics",
    report: "Report",
  };
  return labels[route] ?? route;
}

export function documentLocation(
  page: number | null,
  section: string | null,
): string | null {
  const parts: string[] = [];
  if (page !== null) {
    parts.push(`page ${page}`);
  }
  if (section) {
    parts.push(section);
  }
  return parts.length ? parts.join(" · ") : null;
}

export function cellValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function cartesianColumns(
  rows: Array<Record<string, unknown>>,
): string[] {
  const columns: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) {
        columns.push(key);
      }
    }
  }
  return columns;
}
