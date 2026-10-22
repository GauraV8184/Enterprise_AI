import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  BookOpenText,
  BrainCircuit,
  Database,
  FileSearch,
  Files,
  Globe2,
  LayoutDashboard,
  MessageSquareText,
  Network,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Waypoints,
} from "lucide-react";

import type {
  DocumentRecord,
  LlmStatus,
} from "../api/types";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { Badge } from "../components/Badge";
import { DocumentStatusBadge } from "../components/StatusBadges";
import { EmptyState } from "../components/EmptyState";
import { ErrorNotice } from "../components/ErrorNotice";
import { PageHeader } from "../components/PageHeader";
import { Spinner } from "../components/Spinner";
import { useApiHealth } from "../hooks/useApiHealth";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { formatBytes, formatNumber, formatRelativeTime } from "../lib/format";

const QUICK_PROMPTS = [
  "Summarise the key points in my latest document.",
  "Which product category generated the most revenue?",
  "Create a briefing on the latest developments in enterprise AI.",
];

const CAPABILITIES = [
  {
    to: "/documents",
    icon: FileSearch,
    title: "Document intelligence",
    description:
      "Upload policies, reports and spreadsheets, then ask questions with citations to the exact passage.",
    accent: "teal",
  },
  {
    to: "/ask",
    icon: Waypoints,
    title: "Supervisor workflow",
    description:
      "One request, routed automatically to conversation, retrieval, analytics, research or a report.",
    accent: "indigo",
  },
  {
    to: "/reports",
    icon: BookOpenText,
    title: "Evidence-backed reports",
    description:
      "Generate a Markdown deliverable and inspect every document, web and SQL source it used.",
    accent: "amber",
  },
  {
    to: "/chat",
    icon: MessageSquareText,
    title: "Direct model chat",
    description:
      "A clean conversation surface for ungrounded drafting, explanation and brainstorming.",
    accent: "slate",
  },
];

interface DashboardData {
  documents: DocumentRecord[];
  documentsLoading: boolean;
  documentsError: unknown;
  llm: LlmStatus | null;
  llmLoading: boolean;
  llmError: unknown;
}

export function DashboardPage() {
  useDocumentTitle("Overview");
  const { api, user, mode } = useAuth();
  const health = useApiHealth(api);
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState("");
  const [data, setData] = useState<DashboardData>({
    documents: [],
    documentsLoading: true,
    documentsError: null,
    llm: null,
    llmLoading: true,
    llmError: null,
  });

  const load = useCallback(() => {
    setData((current) => ({
      ...current,
      documentsLoading: true,
      documentsError: null,
      llmLoading: true,
      llmError: null,
    }));

    void api
      .listDocuments()
      .then((response) => {
        setData((current) => ({
          ...current,
          documents: response.documents,
          documentsLoading: false,
          documentsError: null,
        }));
      })
      .catch((error: unknown) => {
        setData((current) => ({
          ...current,
          documentsLoading: false,
          documentsError: error,
        }));
      });

    void api
      .getLlmStatus()
      .then((llm) => {
        setData((current) => ({
          ...current,
          llm,
          llmLoading: false,
          llmError: null,
        }));
      })
      .catch((error: unknown) => {
        setData((current) => ({
          ...current,
          llm: null,
          llmLoading: false,
          llmError: error,
        }));
      });
  }, [api]);

  useEffect(() => {
    load();
  }, [load]);

  const stats = useMemo(() => {
    const ready = data.documents.filter((doc) => doc.status === "ready");
    const failed = data.documents.filter((doc) => doc.status === "failed");
    const chunks = ready.reduce((total, doc) => total + doc.chunk_count, 0);
    return {
      total: data.documents.length,
      ready: ready.length,
      failed: failed.length,
      chunks,
    };
  }, [data.documents]);

  const recentDocuments = data.documents.slice(0, 4);

  function ask(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = prompt.trim();
    if (!value) {
      return;
    }
    navigate(`/ask?q=${encodeURIComponent(value)}`);
  }

  function applyPrompt(value: string) {
    setPrompt(value);
  }

  const displayName = user?.name.trim() ?? "";
  const firstName =
    displayName && !displayName.includes("@")
      ? displayName.split(/\s+/)[0]
      : null;

  return (
    <div className="page dashboard-page">
      <PageHeader
        eyebrow="Overview"
        title={firstName ? `Good to see you, ${firstName}.` : "Good to see you."}
        description="Ask across your knowledge base, business data and the web — with the evidence attached."
        actions={
          <>
            <button type="button" className="btn btn-secondary" onClick={load}>
              <RefreshCw size={16} />
              Refresh
            </button>
            <Link to="/documents" className="btn btn-primary">
              <Plus size={17} />
              Add documents
            </Link>
          </>
        }
      />

      {mode === "development" && (
        <div className="dev-banner">
          <span className="dev-banner-icon">
            <LayoutDashboard size={17} />
          </span>
          <div>
            <strong>Development identity in use</strong>
            <p>
              Requests are authenticated with the <code>X-User-Id</code> header
              as <code>{user?.id}</code>. This is a local-only stub, not a
              security control.
            </p>
          </div>
          <Link to="/settings" className="btn btn-ghost btn-sm">
            Change identity
            <ArrowRight size={15} />
          </Link>
        </div>
      )}

      <section className="hero-card">
        <div className="hero-copy">
          <div className="hero-eyebrow">
            <Sparkles size={15} />
            Ask once. Route intelligently.
          </div>
          <h2>What would you like to understand?</h2>
          <p>
            The supervisor decides whether to search your documents, query the
            demo dataset, research the web, or assemble a report — then shows
            you why.
          </p>

          <form className="hero-composer" onSubmit={ask}>
            <Search size={19} className="hero-composer-icon" />
            <input
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="e.g. Compare our leave policy with market practice…"
              aria-label="Ask the supervisor"
              maxLength={4000}
            />
            <button
              type="submit"
              className="btn btn-primary btn-icon"
              disabled={!prompt.trim()}
              aria-label="Send to supervisor"
            >
              <Send size={18} />
            </button>
          </form>

          <div className="prompt-suggestions" aria-label="Example questions">
            {QUICK_PROMPTS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                className="prompt-chip"
                onClick={() => applyPrompt(suggestion)}
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>

        <div className="route-preview" aria-label="Available routes">
          <div className="route-preview-core">
            <Network size={20} />
            <span>Supervisor</span>
          </div>
          <div className="route-preview-grid">
            <span><Files size={14} /> Documents</span>
            <span><Database size={14} /> Analytics</span>
            <span><Globe2 size={14} /> Research</span>
            <span><BookOpenText size={14} /> Reports</span>
          </div>
          <p>Every route returns evidence, or states that none was found.</p>
        </div>
      </section>

      <div className="stat-grid">
        <article className="stat-card">
          <span className="stat-icon stat-icon-teal"><Files size={19} /></span>
          <div>
            <span className="stat-label">Documents</span>
            <strong>{data.documentsLoading ? "—" : formatNumber(stats.total)}</strong>
            <small>
              {data.documentsLoading
                ? "Loading library"
                : `${stats.ready} ready · ${stats.failed} failed`}
            </small>
          </div>
        </article>
        <article className="stat-card">
          <span className="stat-icon stat-icon-indigo"><BrainCircuit size={19} /></span>
          <div>
            <span className="stat-label">Searchable chunks</span>
            <strong>{data.documentsLoading ? "—" : formatNumber(stats.chunks)}</strong>
            <small>Vectors available for retrieval</small>
          </div>
        </article>
        <article className="stat-card">
          <span className="stat-icon stat-icon-amber"><MessageSquareText size={19} /></span>
          <div>
            <span className="stat-label">Answering model</span>
            <strong className="stat-model">
              {data.llmLoading ? "Checking…" : data.llm?.model ?? "Unavailable"}
            </strong>
            <small>
              {data.llm?.configured
                ? `${data.llm.provider} · ${data.llm.timeout_seconds}s timeout`
                : "Check provider configuration"}
            </small>
          </div>
        </article>
        <article className="stat-card">
          <span className="stat-icon stat-icon-emerald"><ShieldCheck size={19} /></span>
          <div>
            <span className="stat-label">API health</span>
            <strong className="stat-health">
              {health.status === "online" ? "Online" : health.status === "checking" ? "Checking" : "Offline"}
            </strong>
            <small>
              {health.health
                ? `v${health.health.version} · ${health.health.environment}`
                : health.error ?? "Contacting the backend"}
            </small>
          </div>
        </article>
      </div>

      {data.documentsError !== null && (
        <ErrorNotice
          error={data.documentsError}
          title="The document library could not be loaded"
          onRetry={load}
        />
      )}

      <div className="dashboard-grid">
        <section className="panel capabilities-panel">
          <header className="panel-header">
            <div className="panel-title-wrap">
              <span className="panel-icon"><Sparkles size={18} /></span>
              <div>
                <h2>Capabilities</h2>
                <p>Pick the surface that fits the job.</p>
              </div>
            </div>
          </header>
          <div className="capability-list">
            {CAPABILITIES.map((capability) => {
              const Icon = capability.icon;
              return (
                <Link
                  key={capability.to}
                  to={capability.to}
                  className="capability-card"
                >
                  <span className={`capability-icon capability-${capability.accent}`}>
                    <Icon size={19} />
                  </span>
                  <span className="capability-copy">
                    <strong>{capability.title}</strong>
                    <span>{capability.description}</span>
                  </span>
                  <ArrowRight size={17} className="capability-arrow" />
                </Link>
              );
            })}
          </div>
        </section>

        <div className="dashboard-side">
          <section className="panel">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><LayoutDashboard size={18} /></span>
                <div>
                  <h2>Recent documents</h2>
                  <p>Newest uploads in your workspace.</p>
                </div>
              </div>
              <Link to="/documents" className="text-link">
                View all <ArrowRight size={14} />
              </Link>
            </header>

            <div className="recent-doc-list">
              {data.documentsLoading ? (
                <div className="loading-row">
                  <Spinner size={17} />
                  <span>Loading your library…</span>
                </div>
              ) : recentDocuments.length === 0 ? (
                <EmptyState
                  compact
                  icon={<UploadCloud size={22} />}
                  title="No documents yet"
                  description="Upload a PDF, DOCX, XLSX, CSV, TXT or Markdown file to start searching your own knowledge."
                  action={
                    <Link to="/documents" className="btn btn-primary btn-sm">
                      <UploadCloud size={15} />
                      Upload a document
                    </Link>
                  }
                />
              ) : (
                recentDocuments.map((doc) => (
                  <Link
                    key={doc.id}
                    to={`/documents?document=${encodeURIComponent(doc.id)}`}
                    className="recent-doc"
                  >
                    <span className="recent-doc-icon"><Files size={17} /></span>
                    <span className="recent-doc-copy">
                      <strong>{doc.filename}</strong>
                      <small>
                        {formatBytes(doc.size_bytes)} · {formatRelativeTime(doc.created_at)}
                      </small>
                    </span>
                    <DocumentStatusBadge status={doc.status} />
                  </Link>
                ))
              )}
            </div>
          </section>

          <section className="panel system-panel">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><BrainCircuit size={18} /></span>
                <div>
                  <h2>Runtime</h2>
                  <p>What the backend reports right now.</p>
                </div>
              </div>
            </header>
            <div className="system-rows">
              <div className="system-row">
                <span>Environment</span>
                <strong>{health.health?.environment ?? "—"}</strong>
              </div>
              <div className="system-row">
                <span>Service</span>
                <strong>{health.health?.service ?? "—"}</strong>
              </div>
              <div className="system-row">
                <span>LLM provider</span>
                <strong>{data.llm?.provider ?? "—"}</strong>
              </div>
              <div className="system-row">
                <span>Model</span>
                <strong className="mono">{data.llm?.model ?? "—"}</strong>
              </div>
              <div className="system-row">
                <span>Web research</span>
                <Badge tone="neutral">Requires Tavily key</Badge>
              </div>
            </div>
            {data.llmError !== null && (
              <ErrorNotice
                error={data.llmError}
                compact
                title="Could not read model status"
                onRetry={load}
              />
            )}
            {data.llm && !data.llm.configured && (
              <p className="muted-note">
                {data.llm.detail ?? "The LLM is not configured on the backend."}
              </p>
            )}
            {health.status === "offline" && (
              <p className="muted-note">{errorMessage(health.error)}</p>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
