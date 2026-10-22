import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowUpRight,
  Bot,
  CheckCircle2,
  Database,
  ExternalLink,
  Fingerprint,
  KeyRound,
  LogOut,
  Network,
  ShieldCheck,
  Sparkles,
  UserRound,
} from "lucide-react";

import type { LlmStatus } from "../api/types";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { Badge } from "../components/Badge";
import { CopyButton } from "../components/CopyButton";
import { ErrorNotice } from "../components/ErrorNotice";
import { PageHeader } from "../components/PageHeader";
import { Spinner } from "../components/Spinner";
import { useApiHealth } from "../hooks/useApiHealth";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import {
  API_BASE_URL,
  API_DOCS_URL,
  MAX_UPLOAD_MB,
  SUPABASE_URL,
} from "../lib/config";

export function SettingsPage() {
  useDocumentTitle("Settings");
  const { mode, user, api, devUserId, setDevUserId, signOut } = useAuth();
  const health = useApiHealth(api);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [llmError, setLlmError] = useState<unknown>(null);
  const [devId, setDevId] = useState(devUserId);
  const [devBusy, setDevBusy] = useState(false);
  const [devError, setDevError] = useState<string | null>(null);
  const [devSuccess, setDevSuccess] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void api
      .getLlmStatus()
      .then((status) => {
        if (active) {
          setLlm(status);
          setLlmError(null);
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setLlmError(caught);
        }
      });
    return () => {
      active = false;
    };
  }, [api]);

  function saveDevId(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setDevBusy(true);
    setDevError(null);
    setDevSuccess(null);
    try {
      setDevUserId(devId);
      setDevSuccess("Development identity updated. New API requests use this id.");
    } catch (caught) {
      setDevError(errorMessage(caught));
    } finally {
      setDevBusy(false);
    }
  }

  async function handleSignOut() {
    setSigningOut(true);
    setSignOutError(null);
    try {
      await signOut();
    } catch (caught) {
      setSignOutError(errorMessage(caught));
    } finally {
      setSigningOut(false);
    }
  }

  const supabaseHost = (() => {
    if (!SUPABASE_URL) {
      return null;
    }
    try {
      return new URL(SUPABASE_URL).host;
    } catch {
      return SUPABASE_URL;
    }
  })();

  return (
    <div className="page settings-page">
      <PageHeader
        eyebrow="Workspace"
        title="Settings"
        description="Connection details, identity and the boundaries that keep this workspace safe."
      />

      <div className="settings-grid">
        <section className="panel">
          <header className="panel-header">
            <div className="panel-title-wrap">
              <span className="panel-icon"><UserRound size={18} /></span>
              <div>
                <h2>Account &amp; identity</h2>
                <p>Who the backend is acting for on this request.</p>
              </div>
            </div>
            <Badge tone={mode === "supabase" ? "success" : "warning"}>
              {mode === "supabase" ? "Supabase Auth" : "Development stub"}
            </Badge>
          </header>
          <div className="settings-body">
            <dl className="settings-list">
              <div>
                <dt>Display name</dt>
                <dd>{user?.name ?? "—"}</dd>
              </div>
              <div>
                <dt>Email</dt>
                <dd>{user?.email ?? "Not provided"}</dd>
              </div>
              <div>
                <dt>User id</dt>
                <dd className="mono settings-break">{user?.id ?? "—"}</dd>
              </div>
              <div>
                <dt>Authentication</dt>
                <dd>
                  {mode === "supabase"
                    ? "Supabase access token (verified against JWKS)"
                    : "X-User-Id request header"}
                </dd>
              </div>
              {mode === "supabase" && (
                <div>
                  <dt>Supabase project</dt>
                  <dd className="mono">{supabaseHost ?? "—"}</dd>
                </div>
              )}
            </dl>

            {mode === "supabase" ? (
              <div className="settings-action-row">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => void handleSignOut()}
                  disabled={signingOut}
                >
                  {signingOut ? <Spinner size={16} label="Signing out" /> : <LogOut size={16} />}
                  Sign out
                </button>
                {signOutError && <span className="field-error">{signOutError}</span>}
              </div>
            ) : (
              <p className="muted-note">
                The development stub is accepted only while the backend runs in{" "}
                <code>development</code> or <code>test</code>. It is not a
                security control.
              </p>
            )}
          </div>
        </section>

        <section className="panel">
          <header className="panel-header">
            <div className="panel-title-wrap">
              <span className="panel-icon"><Network size={18} /></span>
              <div>
                <h2>API connection</h2>
                <p>The browser talks only to this FastAPI origin.</p>
              </div>
            </div>
            <Badge tone={health.status === "online" ? "success" : health.status === "checking" ? "info" : "danger"}>
              {health.status === "online" ? "Connected" : health.status === "checking" ? "Checking" : "Offline"}
            </Badge>
          </header>
          <div className="settings-body">
            <dl className="settings-list">
              <div>
                <dt>API base URL</dt>
                <dd className="mono settings-break">{API_BASE_URL || "(same origin)"}</dd>
              </div>
              <div>
                <dt>Environment</dt>
                <dd>{health.health?.environment ?? "—"}</dd>
              </div>
              <div>
                <dt>Service version</dt>
                <dd>{health.health?.version ?? "—"}</dd>
              </div>
              <div>
                <dt>Upload limit</dt>
                <dd>{MAX_UPLOAD_MB} MB (client hint; server enforces)</dd>
              </div>
            </dl>
            <div className="settings-action-row">
              <CopyButton text={API_BASE_URL || window.location.origin} label="Copy API URL" />
              <a className="btn btn-ghost btn-sm" href={API_DOCS_URL} target="_blank" rel="noreferrer noopener">
                <ExternalLink size={15} />
                Open API docs
              </a>
            </div>
            {health.status === "offline" && health.error && (
              <div className="inline-alert inline-alert-error" role="alert">
                {health.error}
              </div>
            )}
          </div>
        </section>

        <section className="panel">
          <header className="panel-header">
            <div className="panel-title-wrap">
              <span className="panel-icon"><Bot size={18} /></span>
              <div>
                <h2>Model &amp; evidence</h2>
                <p>Provider details reported by the backend.</p>
              </div>
            </div>
          </header>
          <div className="settings-body">
            {llmError !== null ? (
              <ErrorNotice error={llmError} compact title="Model status unavailable" />
            ) : llm ? (
              <dl className="settings-list">
                <div><dt>Provider</dt><dd>{llm.provider}</dd></div>
                <div><dt>Model</dt><dd className="mono">{llm.model}</dd></div>
                <div><dt>Configured</dt><dd>{llm.configured ? "Yes" : "No"}</dd></div>
                <div><dt>Timeout</dt><dd>{llm.timeout_seconds} seconds</dd></div>
                <div><dt>Retries</dt><dd>{llm.max_retries}</dd></div>
              </dl>
            ) : (
              <div className="loading-row"><Spinner size={17} /><span>Reading model status…</span></div>
            )}
            {llm && !llm.configured && (
              <div className="inline-alert inline-alert-warning">
                {llm.detail ?? "The backend has no usable model configuration."}
              </div>
            )}
          </div>
        </section>

        {mode === "development" && (
          <section className="panel settings-dev-panel">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><Fingerprint size={18} /></span>
                <div>
                  <h2>Development identity</h2>
                  <p>Switch the local tenant without creating an account.</p>
                </div>
              </div>
            </header>
            <div className="settings-body">
              <form className="stack-form" onSubmit={saveDevId}>
                <label className="field">
                  <span>X-User-Id</span>
                  <input
                    value={devId}
                    onChange={(event) => setDevId(event.target.value)}
                    maxLength={128}
                    required
                    placeholder="dev-user"
                  />
                  <small className="field-hint">
                    A new id sees a different document library, because every
                    query is scoped by this value in development.
                  </small>
                </label>
                {devError && <p className="field-error">{devError}</p>}
                {devSuccess && (
                  <div className="inline-alert inline-alert-success">
                    <CheckCircle2 size={16} />
                    {devSuccess}
                  </div>
                )}
                <button type="submit" className="btn btn-secondary" disabled={devBusy}>
                  {devBusy ? <Spinner size={16} /> : <Fingerprint size={16} />}
                  Save identity
                </button>
              </form>
            </div>
          </section>
        )}

        <section className="panel settings-security">
          <header className="panel-header">
            <div className="panel-title-wrap">
              <span className="panel-icon"><ShieldCheck size={18} /></span>
              <div>
                <h2>Security boundary</h2>
                <p>What this browser is allowed to hold.</p>
              </div>
            </div>
          </header>
          <div className="settings-body">
            <div className="security-points">
              <div>
                <span><KeyRound size={17} /></span>
                <p>
                  <strong>Only the public anon key</strong>
                  The Supabase anon key can be held by a browser because RLS
                  still decides every row. The service-role key never leaves
                  the backend.
                </p>
              </div>
              <div>
                <span><Database size={17} /></span>
                <p>
                  <strong>RLS is the boundary</strong>
                  Ownership is enforced in the database, not by hiding
                  buttons. A request for another tenant&apos;s document returns
                  404.
                </p>
              </div>
              <div>
                <span><Sparkles size={17} /></span>
                <p>
                  <strong>No fabricated fallback</strong>
                  If a model or source is unavailable, the API returns a typed
                  error or a clearly marked no-evidence response.
                </p>
              </div>
            </div>
          </div>
        </section>
      </div>

      <footer className="settings-footer">
        <span>Enterprise AI Intelligence Platform</span>
        <a href="https://github.com/GauraV8184/Enterprise_AI" target="_blank" rel="noreferrer noopener">
          Source repository <ArrowUpRight size={14} />
        </a>
      </footer>
    </div>
  );
}
