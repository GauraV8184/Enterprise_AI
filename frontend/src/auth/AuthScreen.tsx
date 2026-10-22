import { useState, type FormEvent } from "react";
import {
  ArrowRight,
  BookOpenText,
  CheckCircle2,
  Database,
  FileSearch,
  Globe2,
  Loader2,
  LockKeyhole,
  Network,
  Sparkles,
} from "lucide-react";

import { errorMessage } from "../api/errors";
import { APP_NAME } from "../lib/config";
import { useAuth } from "./useAuth";

type AuthView = "signin" | "signup";

export function AuthScreen() {
  const { mode, signIn, signUp, sessionError } = useAuth();
  const [view, setView] = useState<AuthView>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const isMisconfigured = mode === "misconfigured";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSuccess(null);

    if (view === "signup" && password !== confirmPassword) {
      setError("The passwords do not match.");
      return;
    }
    if (password.length < 6) {
      setError("Use a password with at least 6 characters.");
      return;
    }

    setBusy(true);
    try {
      if (view === "signin") {
        await signIn(email.trim(), password);
      } else {
        const result = await signUp(email.trim(), password);
        if (result.needsConfirmation) {
          setSuccess(
            "Account created. Check your email for the confirmation link, then sign in.",
          );
          setView("signin");
          setPassword("");
          setConfirmPassword("");
        }
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function switchView(next: AuthView) {
    setView(next);
    setError(null);
    setSuccess(null);
    setPassword("");
    setConfirmPassword("");
  }

  return (
    <main className="auth-page">
      <section className="auth-story" aria-label="Platform overview">
        <div className="auth-brand">
          <span className="brand-mark" aria-hidden="true">
            <Sparkles size={20} strokeWidth={2} />
          </span>
          <span>{APP_NAME}</span>
        </div>

        <div className="auth-story-copy">
          <p className="eyebrow">One workspace · grounded answers</p>
          <h1>Turn scattered knowledge into a decision you can defend.</h1>
          <p className="auth-lead">
            Ask across internal documents, structured business data, and the
            live web. Every answer carries the evidence behind it.
          </p>

          <div className="auth-feature-grid">
            <div>
              <FileSearch size={19} />
              <span>Cited document Q&amp;A</span>
            </div>
            <div>
              <Network size={19} />
              <span>Supervisor routing</span>
            </div>
            <div>
              <Database size={19} />
              <span>Read-only analytics</span>
            </div>
            <div>
              <Globe2 size={19} />
              <span>Live web research</span>
            </div>
            <div>
              <BookOpenText size={19} />
              <span>Evidence-backed reports</span>
            </div>
            <div>
              <LockKeyhole size={19} />
              <span>Tenant isolation by RLS</span>
            </div>
          </div>
        </div>

        <p className="auth-footnote">
          The browser holds only the public Supabase anon key. The service-role
          key and model credentials stay on the backend.
        </p>
      </section>

      <section className="auth-panel" aria-label="Sign in">
        <div className="auth-card">
          <div className="auth-mobile-brand" aria-hidden="true">
            <span className="brand-mark brand-mark-sm">
              <Sparkles size={15} strokeWidth={2} />
            </span>
            <span>
              <strong>Enterprise AI</strong>
              <small>Intelligence Platform</small>
            </span>
          </div>

          {isMisconfigured ? (
            <div className="auth-config">
              <span className="icon-chip icon-chip-warning">
                <LockKeyhole size={20} />
              </span>
              <h2>Supabase configuration is incomplete</h2>
              <p>
                Both <code>VITE_SUPABASE_URL</code> and{" "}
                <code>VITE_SUPABASE_ANON_KEY</code> are required. Set both in{" "}
                <code>frontend/.env.local</code> and restart the dev server, or
                leave both empty to use the development identity stub.
              </p>
            </div>
          ) : (
            <>
              <div className="auth-heading">
                <p className="eyebrow">
                  {view === "signin" ? "Welcome back" : "Create an account"}
                </p>
                <h2>
                  {view === "signin"
                    ? "Sign in to your workspace"
                    : "Start your workspace"}
                </h2>
                <p>
                  {view === "signin"
                    ? "Use the email and password for your Supabase account."
                    : "Accounts are managed by Supabase Auth. Email confirmation may be required."}
                </p>
              </div>

              <div className="segmented" role="tablist" aria-label="Auth mode">
                <button
                  type="button"
                  role="tab"
                  aria-selected={view === "signin"}
                  className={view === "signin" ? "active" : ""}
                  onClick={() => switchView("signin")}
                >
                  Sign in
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={view === "signup"}
                  className={view === "signup" ? "active" : ""}
                  onClick={() => switchView("signup")}
                >
                  Create account
                </button>
              </div>

              <form className="stack-form" onSubmit={handleSubmit}>
                <label className="field">
                  <span>Email</span>
                  <input
                    type="email"
                    autoComplete="email"
                    required
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="you@company.com"
                  />
                </label>

                <label className="field">
                  <span>Password</span>
                  <input
                    type="password"
                    autoComplete={
                      view === "signin" ? "current-password" : "new-password"
                    }
                    required
                    minLength={6}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="At least 6 characters"
                  />
                </label>

                {view === "signup" && (
                  <label className="field">
                    <span>Confirm password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      required
                      minLength={6}
                      value={confirmPassword}
                      onChange={(event) =>
                        setConfirmPassword(event.target.value)
                      }
                      placeholder="Repeat your password"
                    />
                  </label>
                )}

                {(error || sessionError) && (
                  <div className="inline-alert inline-alert-error" role="alert">
                    {error ?? sessionError}
                  </div>
                )}
                {success && (
                  <div
                    className="inline-alert inline-alert-success"
                    role="status"
                  >
                    <CheckCircle2 size={17} />
                    <span>{success}</span>
                  </div>
                )}

                <button
                  type="submit"
                  className="btn btn-primary btn-block"
                  disabled={busy}
                >
                  {busy ? (
                    <>
                      <Loader2 className="spin" size={18} />
                      {view === "signin" ? "Signing in…" : "Creating account…"}
                    </>
                  ) : (
                    <>
                      {view === "signin" ? "Sign in" : "Create account"}
                      <ArrowRight size={18} />
                    </>
                  )}
                </button>
              </form>

              <p className="auth-secure-note">
                <LockKeyhole size={14} />
                Authentication is verified by the backend against Supabase&apos;s
                published keys.
              </p>
            </>
          )}
        </div>
      </section>
    </main>
  );
}
