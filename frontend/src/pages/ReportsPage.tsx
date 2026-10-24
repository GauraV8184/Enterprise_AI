import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  AlertTriangle,
  BookOpenText,
  Check,
  ChevronRight,
  Database,
  Download,
  FileText,
  Globe2,
  Loader2,
  Printer,
  Sparkles,
  WandSparkles,
  X,
} from "lucide-react";

import type { ReportResponse, ReportSource } from "../api/types";
import { isAbortError, isApiError, retryHint } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { Badge } from "../components/Badge";
import { CopyButton } from "../components/CopyButton";
import { EmptyState } from "../components/EmptyState";
import { ErrorNotice } from "../components/ErrorNotice";
import { EvidenceSections, ResponseMeta } from "../components/EvidencePanel";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useTransientFlag } from "../hooks/useTransientFlag";
import { formatNumber } from "../lib/format";

const MAX_TOPIC_CHARS = 4000;

const SOURCE_META: Array<{
  source: ReportSource;
  icon: typeof FileText;
  title: string;
  description: string;
  note: string;
}> = [
  {
    source: "documents",
    icon: FileText,
    title: "Documents",
    description: "Search your own indexed corpus.",
    note: "Needs at least one ready document.",
  },
  {
    source: "analytics",
    icon: Database,
    title: "Analytics",
    description: "Run one read-only query over demo sales data.",
    note: "Clearly labelled demonstration data.",
  },
  {
    source: "web",
    icon: Globe2,
    title: "Web research",
    description: "Add live results from Tavily.",
    note: "Requires TAVILY_API_KEY on the backend.",
  },
];

const TOPIC_SUGGESTIONS = [
  "Summarise the operational risks in our policy library.",
  "Analyse revenue by product category and recommend the next focus.",
  "Create an executive briefing on current AI regulation.",
];

function makeFilename(topic: string): string {
  const base = topic
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return `${base || "enterprise-ai-report"}.md`;
}

function reportErrorHint(error: unknown): string {
  if (isApiError(error) && error.code === "analytics_query_failed") {
    return "The analytics agent could not produce a valid read-only query. The report was not written; rephrase the topic or retry.";
  }
  if (isApiError(error)) {
    return (
      retryHint(error) ??
      "The requested report was not written. Check the error, then try again."
    );
  }
  return "The request did not complete, so no report was written. Check the error and try again.";
}

export function ReportsPage() {
  useDocumentTitle("Reports");
  const { api } = useAuth();
  const [topic, setTopic] = useState("");
  const [sources, setSources] = useState<ReportSource[]>(["documents"]);
  const [result, setResult] = useState<ReportResponse | null>(null);
  const [generatedAt, setGeneratedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const abortRef = useRef<AbortController | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const highlightReport = useTransientFlag(busy ? null : result);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  function toggleSource(source: ReportSource) {
    setSources((current) =>
      current.includes(source)
        ? current.filter((item) => item !== source)
        : [...current, source],
    );
  }

  const generate = useCallback(
    async (event?: FormEvent<HTMLFormElement>) => {
      event?.preventDefault();
      const value = topic.trim();
      if (!value || sources.length === 0 || busy) {
        return;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      setBusy(true);
      setError(null);
      setResult(null);
      setGeneratedAt(null);

      const orderedSources = SOURCE_META.map((meta) => meta.source).filter(
        (source) => sources.includes(source),
      );

      try {
        const response = await api.generateReport(
          { topic: value, sources: orderedSources },
          controller.signal,
        );
        setResult(response);
        setGeneratedAt(new Date());
        window.setTimeout(() => {
          resultRef.current?.scrollIntoView({
            behavior: "smooth",
            block: "start",
          });
        }, 40);
      } catch (caught) {
        if (!isAbortError(caught)) {
          setError(caught);
        }
      } finally {
        abortRef.current = null;
        setBusy(false);
      }
    },
    [api, busy, sources, topic],
  );

  function cancel() {
    abortRef.current?.abort();
    setBusy(false);
  }

  function download() {
    if (!result) {
      return;
    }
    const blob = new Blob([result.report], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = makeFilename(result.topic);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function reset() {
    setResult(null);
    setError(null);
    setGeneratedAt(null);
    setTopic("");
    setSources(["documents"]);
  }

  const canGenerate =
    topic.trim().length > 0 && sources.length > 0 && !busy;

  return (
    <div className="page reports-page">
      <PageHeader
        eyebrow="Deliverables"
        title="Reports"
        description="Assemble a Markdown report from your documents, the demo dataset and live web research — with every source returned alongside it."
        actions={
          result ? (
            <button type="button" className="btn btn-secondary" onClick={reset}>
              <WandSparkles size={16} />
              New report
            </button>
          ) : undefined
        }
      />

      <div className="reports-layout">
        <aside className="report-builder">
          <section className="panel report-form-panel">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><BookOpenText size={18} /></span>
                <div>
                  <h2>Build a report</h2>
                  <p>Choose the evidence, then describe the deliverable.</p>
                </div>
              </div>
            </header>
            <form className="report-form" onSubmit={generate}>
              <label className="field">
                <span>Topic</span>
                <textarea
                  value={topic}
                  onChange={(event) => setTopic(event.target.value)}
                  placeholder="What should the report cover?"
                  rows={5}
                  maxLength={MAX_TOPIC_CHARS}
                  disabled={busy}
                />
                <small className="field-hint">
                  {topic.length > 3600
                    ? `${formatNumber(topic.length)} / ${formatNumber(MAX_TOPIC_CHARS)}`
                    : "Be specific about the decision this report should support."}
                </small>
              </label>

              <fieldset className="source-fieldset" disabled={busy}>
                <legend>Evidence sources</legend>
                <div className="source-options">
                  {SOURCE_META.map((meta) => {
                    const Icon = meta.icon;
                    const checked = sources.includes(meta.source);
                    return (
                      <label
                        key={meta.source}
                        className={`source-option ${checked ? "is-checked" : ""}`.trim()}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleSource(meta.source)}
                          disabled={busy}
                        />
                        <span className="source-option-icon"><Icon size={18} /></span>
                        <span className="source-option-copy">
                          <strong>{meta.title}</strong>
                          <span>{meta.description}</span>
                          <small>{meta.note}</small>
                        </span>
                        <span className="source-check" aria-hidden="true">
                          {checked && <Check size={14} />}
                        </span>
                      </label>
                    );
                  })}
                </div>
                {sources.length === 0 && (
                  <p className="field-error">Choose at least one source.</p>
                )}
              </fieldset>

              <div className="report-submit-row">
                {busy ? (
                  <button type="button" className="btn btn-secondary" onClick={cancel}>
                    <X size={16} />
                    Cancel
                  </button>
                ) : (
                  <button type="submit" className="btn btn-primary btn-block" disabled={!canGenerate}>
                    <Sparkles size={17} />
                    Generate report
                  </button>
                )}
              </div>
              <p className="field-hint">
                The response includes the report and the exact evidence it was
                written from.
              </p>
            </form>
          </section>

          <section className="rail-card rail-card-soft report-tips">
            <div className="rail-heading"><ChevronRight size={16} /> Good topics</div>
            <div className="suggestion-stack">
              {TOPIC_SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  className="suggestion-link"
                  onClick={() => setTopic(suggestion)}
                  disabled={busy}
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </section>
        </aside>

        <div className="report-result-column" ref={resultRef}>
          {busy && (
            <section className="panel report-loading">
              <span className="report-loading-icon"><Loader2 className="spin" size={25} /></span>
              <h2>Assembling the report</h2>
              <p>
                Gathering the requested evidence, validating any SQL, and
                asking the model to write only from what was found.
              </p>
              <div className="report-progress">
                <span className="active">Gather evidence</span>
                <span>Check grounding</span>
                <span>Write report</span>
              </div>
            </section>
          )}

          {!busy && !result && error === null && (
            <section className="panel report-empty-panel">
              <EmptyState
                icon={<BookOpenText size={27} />}
                title="Evidence first, prose second"
                description="A report never widens its own scope. If every requested source comes back empty, the response says so and the model is not asked to write."
                action={
                  <div className="report-flow">
                    <div><span>1</span><strong>Choose sources</strong><small>Documents, SQL, web</small></div>
                    <ChevronRight size={16} />
                    <div><span>2</span><strong>Collect evidence</strong><small>With labels and rows</small></div>
                    <ChevronRight size={16} />
                    <div><span>3</span><strong>Write &amp; inspect</strong><small>Markdown + sources</small></div>
                  </div>
                }
              />
            </section>
          )}

          {error !== null && result === null && !busy && (
            <section className="panel">
              <ErrorNotice
                error={error}
                title="No report was produced"
                onRetry={() => void generate()}
              />
              <p className="muted-note">{reportErrorHint(error)}</p>
            </section>
          )}

          {result && (
            <section
              className={`report-output${
                highlightReport ? " answer-highlight" : ""
              }`}
            >
              <div className="report-output-toolbar">
                <div>
                  <p className="eyebrow">Markdown report</p>
                  <h2>{result.topic}</h2>
                  <div className="report-meta-row">
                    <Badge tone={result.grounded ? "success" : "warning"}>
                      {result.grounded ? "Grounded" : "No evidence found"}
                    </Badge>
                    {result.sources_requested.map((source) => (
                      <Badge key={source} tone="neutral">
                        {source}
                      </Badge>
                    ))}
                    {generatedAt && (
                      <span className="muted-inline">
                        Generated {generatedAt.toLocaleString()}
                      </span>
                    )}
                  </div>
                </div>
                <div className="report-toolbar-actions">
                  <CopyButton text={result.report} label="Copy" />
                  <button type="button" className="btn btn-ghost btn-sm" onClick={download}>
                    <Download size={15} />
                    Download .md
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => window.print()}
                  >
                    <Printer size={15} />
                    Print
                  </button>
                </div>
              </div>

              {!result.grounded && (
                <div className="inline-alert inline-alert-warning">
                  <AlertTriangle size={16} />
                  <span>
                    None of the requested sources returned evidence. The
                    Markdown below was assembled by code to state that gap; the
                    model was not asked to invent content.
                  </span>
                </div>
              )}

              <article className="report-paper">
                <div className="report-paper-brand">
                  <span className="brand-mark brand-mark-sm"><Sparkles size={15} /></span>
                  <span>Enterprise AI Intelligence Platform</span>
                </div>
                <Markdown className="report-markdown">{result.report}</Markdown>
              </article>

              <section className="panel report-evidence">
                <header className="panel-header">
                  <div className="panel-title-wrap">
                    <span className="panel-icon"><FileText size={18} /></span>
                    <div>
                      <h2>Evidence behind the report</h2>
                      <p>
                        The same source set the model saw, returned so a reader
                        can check the claims.
                      </p>
                    </div>
                  </div>
                </header>
                <div className="report-evidence-summary">
                  <span><strong>{result.sources.length}</strong> document passages</span>
                  <span><strong>{result.web_sources.length}</strong> web results</span>
                  <span><strong>{result.analytics ? result.analytics.row_count : 0}</strong> analytics rows</span>
                </div>
                {result.sources.length === 0 &&
                result.web_sources.length === 0 &&
                !result.analytics ? (
                  <EmptyState
                    compact
                    icon={<AlertTriangle size={22} />}
                    title="No evidence was returned"
                    description="No document passages, web results or analytics rows backed this report."
                  />
                ) : (
                  <EvidenceSections
                    sources={result.sources}
                    webSources={result.web_sources}
                    analytics={result.analytics}
                  />
                )}
                <ResponseMeta
                  provider={result.provider}
                  model={result.model}
                  usage={result.usage}
                  requestId={result.request_id}
                />
              </section>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
