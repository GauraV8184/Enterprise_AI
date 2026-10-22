import { Suspense, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpenText,
  ChevronRight,
  Files,
  LayoutDashboard,
  LogOut,
  Menu,
  MessagesSquare,
  Settings,
  ShieldCheck,
  Sparkles,
  Waypoints,
  WifiOff,
  X,
} from "lucide-react";

import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { useApiHealth } from "../hooks/useApiHealth";
import { APP_NAME } from "../lib/config";
import { Spinner } from "./Spinner";

const NAV_ITEMS = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/chat", label: "Chat", icon: MessagesSquare },
  { to: "/documents", label: "Documents", icon: Files },
  { to: "/ask", label: "Ask supervisor", icon: Waypoints },
  { to: "/reports", label: "Reports", icon: BookOpenText },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

const PAGE_META: Record<string, { eyebrow: string; title: string }> = {
  "/": { eyebrow: "Workspace", title: "Overview" },
  "/chat": { eyebrow: "Conversation", title: "Chat" },
  "/documents": { eyebrow: "Knowledge base", title: "Documents" },
  "/ask": { eyebrow: "Orchestration", title: "Ask supervisor" },
  "/reports": { eyebrow: "Deliverables", title: "Reports" },
  "/settings": { eyebrow: "Workspace", title: "Settings" },
};

function initials(name: string): string {
  const parts = name
    .split(/\s+/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) {
    return "U";
  }
  return parts
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function HealthChip({
  status,
  environment,
  error,
  onRefresh,
}: {
  status: "checking" | "online" | "offline";
  environment?: string;
  error: string | null;
  onRefresh: () => void;
}) {
  const label =
    status === "checking"
      ? "Connecting"
      : status === "online"
        ? "API online"
        : "API offline";

  return (
    <button
      type="button"
      className={`health-chip health-${status}`}
      onClick={onRefresh}
      title={
        status === "offline"
          ? `${error ?? "The API is unreachable."} Click to retry.`
          : status === "online"
            ? `Connected to the ${environment ?? "configured"} API. Click to re-check.`
            : "Checking the API connection"
      }
    >
      <span className="health-dot" aria-hidden="true" />
      <span>{label}</span>
      {status === "offline" && <WifiOff size={13} />}
    </button>
  );
}

export function AppShell() {
  const { user, mode, signOut, api } = useAuth();
  const health = useApiHealth(api);
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);

  const meta = PAGE_META[location.pathname] ?? {
    eyebrow: "Workspace",
    title: "Not found",
  };

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!sidebarOpen) {
      return;
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setSidebarOpen(false);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  async function handleSignOut() {
    setSigningOut(true);
    setSignOutError(null);
    try {
      await signOut();
    } catch (error) {
      setSignOutError(errorMessage(error));
    } finally {
      setSigningOut(false);
    }
  }

  const displayName = user?.name ?? "Workspace user";
  const displayEmail = user?.email ?? (mode === "development" ? user?.id : "");

  return (
    <div className="app-shell">
      <div
        className={`sidebar-scrim ${sidebarOpen ? "is-visible" : ""}`.trim()}
        onClick={() => setSidebarOpen(false)}
        aria-hidden="true"
      />

      <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`.trim()}>
        <div className="sidebar-top">
          <NavLink to="/" className="brand" aria-label={`${APP_NAME} home`}>
            <span className="brand-mark" aria-hidden="true">
              <Sparkles size={20} strokeWidth={2} />
            </span>
            <span className="brand-copy">
              <strong>Enterprise AI</strong>
              <small>Intelligence Platform</small>
            </span>
          </NavLink>

          <button
            type="button"
            className="icon-button sidebar-close"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close navigation"
          >
            <X size={19} />
          </button>
        </div>

        <nav className="main-nav" aria-label="Main navigation">
          <p className="nav-label">Workspace</p>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={"end" in item ? item.end : false}
                className={({ isActive }) =>
                  `nav-item ${isActive ? "active" : ""}`.trim()
                }
              >
                <Icon size={18} strokeWidth={1.9} />
                <span>{item.label}</span>
                <ChevronRight size={14} className="nav-chevron" />
              </NavLink>
            );
          })}
        </nav>

        <div className="sidebar-spacer" />

        <div className="sidebar-note">
          <span className="sidebar-note-icon">
            <ShieldCheck size={16} />
          </span>
          <p>
            <strong>Grounded by design</strong>
            <span>Answers come with the evidence behind them.</span>
          </p>
        </div>

        <div className="sidebar-footer">
          <button
            type="button"
            className="sidebar-health"
            onClick={health.refresh}
            title="Check API connection"
          >
            <Activity size={14} />
            <span>
              {health.status === "online"
                ? `API ${health.health?.version ?? ""}`
                : health.status === "offline"
                  ? "API unreachable"
                  : "Checking API"}
            </span>
            <span className={`sidebar-health-dot health-${health.status}`} />
          </button>

          <NavLink to="/settings" className="sidebar-user">
            <span className="avatar">{initials(displayName)}</span>
            <span className="sidebar-user-copy">
              <strong>{displayName}</strong>
              <small>{mode === "development" ? "Development identity" : displayEmail}</small>
            </span>
          </NavLink>

          {mode === "supabase" && (
            <button
              type="button"
              className="sidebar-signout"
              onClick={() => void handleSignOut()}
              disabled={signingOut}
            >
              {signingOut ? <Spinner size={15} label="Signing out" /> : <LogOut size={15} />}
              Sign out
            </button>
          )}
          {signOutError && (
            <p className="sidebar-error" role="alert">
              {signOutError}
            </p>
          )}
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="icon-button menu-button"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open navigation"
              aria-expanded={sidebarOpen}
            >
              <Menu size={20} />
            </button>
            <div className="topbar-title">
              <span>{meta.eyebrow}</span>
              <strong>{meta.title}</strong>
            </div>
          </div>

          <div className="topbar-right">
            <HealthChip
              status={health.status}
              environment={health.health?.environment}
              error={health.error}
              onRefresh={health.refresh}
            />
            {mode === "development" && (
              <span className="dev-chip" title="Requests use the X-User-Id development header">
                Dev identity
              </span>
            )}
            <NavLink to="/settings" className="topbar-user" aria-label="Open settings">
              <span className="avatar avatar-sm">{initials(displayName)}</span>
              <span className="topbar-user-copy">
                <strong>{displayName}</strong>
                <small>{mode === "development" ? "Local mode" : "Signed in"}</small>
              </span>
            </NavLink>
          </div>
        </header>

        <main className="content">
          <Suspense
            fallback={
              <div className="route-loading" aria-live="polite">
                <Spinner size={18} />
                <span>Loading view…</span>
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>

        <footer className="app-footer">
          <span>{APP_NAME}</span>
          <span className="app-footer-sep">·</span>
          <span>Evidence over improvisation</span>
        </footer>
      </div>
    </div>
  );
}
