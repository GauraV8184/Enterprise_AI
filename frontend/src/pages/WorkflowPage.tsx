import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  BookOpenText,
  Bot,
  Database,
  FileText,
  Globe2,
  History,
  Network,
  RefreshCw,
  Send,
  Sparkles,
  Trash2,
  Waypoints,
} from "lucide-react";

import type {
  ChatMessage,
  DocumentRecord,
  LlmStatus,
  WorkflowResponse,
  WorkflowRoute,
} from "../api/types";
import { errorMessage, isAbortError } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { Badge } from "../components/Badge";
import { CopyButton } from "../components/CopyButton";
import { EmptyState } from "../components/EmptyState";
import { ErrorNotice } from "../components/ErrorNotice";
import { EvidenceSections, ResponseMeta } from "../components/EvidencePanel";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { GroundedBadge, RouteBadge } from "../components/StatusBadges";
import { Spinner } from "../components/Spinner";
import { useAutoResize } from "../hooks/useAutoResize";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useTransientFlag } from "../hooks/useTransientFlag";
import { formatNumber, humanizeIntent } from "../lib/format";

const MAX_QUESTION_CHARS = 4000;

const ROUTE_LEGEND: Array<{
  route: WorkflowRoute;
  icon: typeof FileText;
  description: string;
}> = [
  { route: "chat", icon: Bot, description: "Conversational answer, no evidence required." },
  { route: "documents", icon: FileText, description: "Grounded in your uploaded corpus." },
  { route: "analytics", icon: Database, description: "Read-only SQL over the demo dataset." },
  { route: "research", icon: Globe2, description: "Live web search; needs TAVILY_API_KEY." },
  { route: "report", icon: BookOpenText, description: "Markdown from the requested sources." },
];

const SAMPLE_PROMPTS = [
  {
    label: "Document question",
    prompt: "Summarise the key themes across my uploaded documents.",
  },
  {
    label: "Analytics",
    prompt: "Which product category generated the highest total revenue?",
  },
  {
    label: "Report",
    prompt: "Write a short report on the revenue performance in the demo dataset.",
  },
  {
    label: "Web research",
    prompt: "What are the latest developments in enterprise AI regulation?",
  },
  {
    label: "General chat",
    prompt: "Explain the difference between precision and recall.",
  },
];

interface WorkflowTurn {
  id: string;
  question: string;
  status: "pending" | "success" | "error";
  response?: WorkflowResponse;
  error?: unknown;
  startedAt: number;
}

function makeTurnId(): string {
  return `turn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function buildHistory(turns: WorkflowTurn[]): ChatMessage[] {
  const history: ChatMessage[] = [];
  for (const turn of turns) {
    if (turn.status === "success" && turn.response) {
      history.push({ role: "user", content: turn.question });
      history.push({ role: "assistant", content: turn.response.answer });
    }
  }
  return history.slice(-48);
}

function IntentChips({ response }: { response: WorkflowResponse }) {
  const labels: string[] = [];
  if (response.intent.requires_internal_documents) {
    labels.push("Internal documents");
  }
  if (response.intent.requires_business_data) {
    labels.push("Business data");
  }
  if (response.intent.requires_web_research) {
    labels.push("Web research");
  }

  return (
    <div className="intent-row">
      <Badge tone="neutral">{humanizeIntent(response.intent.intent)}</Badge>
      <Badge tone="neutral">{response.intent.complexity}</Badge>
      {labels.map((label) => (
        <Badge key={label} tone="info">{label}</Badge>
      ))}
    </div>
  );
}

export function WorkflowPage() {
  useDocumentTitle("Ask supervisor");
  const { api } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialQuestion = searchParams.get("q") ?? "";

  const [input, setInput] = useState(initialQuestion);
  const [turns, setTurns] = useState<WorkflowTurn[]>([]);
  const initialRunRef = useRef<string | null>(null);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [documentsLoading, setDocumentsLoading] = useState(true);
  const [documentsError, setDocumentsError] = useState<unknown>(null);
  const [scopeDocumentId, setScopeDocumentId] = useState("all");
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [llmError, setLlmError] = useState<unknown>(null);
  const textareaRef = useAutoResize(input, 200);

  const pending = turns.some((turn) => turn.status === "pending");

  const loadDocuments = useCallback(async () => {
    setDocumentsLoading(true);
    setDocumentsError(null);
    try {
      const response = await api.listDocuments();
      setDocuments(response.documents);
    } catch (caught) {
      setDocumentsError(caught);
    } finally {
      setDocumentsLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void loadDocuments();
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
  }, [api, loadDocuments]);

  useEffect(() => {
    if (
      !documentsLoading &&
      scopeDocumentId !== "all" &&
      !documents.some(
        (document) =>
          document.id === scopeDocumentId && document.status === "ready",
      )
    ) {
      setScopeDocumentId("all");
    }
  }, [documents, documentsLoading, scopeDocumentId]);

  const readyDocuments = useMemo(
    () => documents.filter((document) => document.status === "ready"),
    [documents],
  );

  const routeCounts = useMemo(() => {
    const counts = new Map<WorkflowRoute, number>();
    for (const turn of turns) {
      if (turn.response) {
        counts.set(
          turn.response.route,
          (counts.get(turn.response.route) ?? 0) + 1,
        );
      }
    }
    return counts;
  }, [turns]);

  const lastSuccessId = useMemo(() => {
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      if (turns[index].status === "success") {
        return turns[index].id;
      }
    }
    return null;
  }, [turns]);
  const highlightAnswer = useTransientFlag(pending ? null : lastSuccessId);

  const runTurn = useCallback(
    async (
      turnId: string,
      question: string,
      priorTurns: WorkflowTurn[],
    ) => {
      const controller = new AbortController();
      setTurns((current) =>
        current.map((turn) =>
          turn.id === turnId
            ? {
                ...turn,
                status: "pending",
                error: undefined,
                response: undefined,
              }
            : turn,
        ),
      );

      try {
        const response = await api.runWorkflow(
          {
            message: question,
            history: buildHistory(priorTurns),
            document_ids:
              scopeDocumentId === "all" ? undefined : [scopeDocumentId],
          },
          controller.signal,
        );
        setTurns((current) =>
          current.map((turn) =>
            turn.id === turnId
              ? { ...turn, status: "success", response, error: undefined }
              : turn,
          ),
        );
      } catch (caught) {
        if (isAbortError(caught)) {
          setTurns((current) =>
            current.map((turn) =>
              turn.id === turnId
                ? {
                    ...turn,
                    status: "error",
                    error: new Error("Request cancelled."),
                  }
                : turn,
            ),
          );
        } else {
          setTurns((current) =>
            current.map((turn) =>
              turn.id === turnId
                ? { ...turn, status: "error", error: caught }
                : turn,
            ),
          );
        }
      }
    },
    [api, scopeDocumentId],
  );

  /**
   * A question handed over from the dashboard runs once automatically. The
   * query parameter is then removed so a refresh or a back navigation does not
   * silently submit it again.
   */
  useEffect(() => {
    const question = initialQuestion.trim();
    if (!question || initialRunRef.current === question) {
      return;
    }
    initialRunRef.current = question;

    const turn: WorkflowTurn = {
      id: makeTurnId(),
      question,
      status: "pending",
      startedAt: Date.now(),
    };
    setTurns((current) => [...current, turn]);
    setInput("");
    void runTurn(turn.id, question, []);
    setSearchParams({}, { replace: true });
  }, [initialQuestion, runTurn, setSearchParams]);

  function submitQuestion(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const question = input.trim();
    if (!question || pending) {
      return;
    }

    const turn: WorkflowTurn = {
      id: makeTurnId(),
      question,
      status: "pending",
      startedAt: Date.now(),
    };
    const priorTurns = turns;
    setTurns((current) => [...current, turn]);
    setInput("");
    void runTurn(turn.id, question, priorTurns);
  }

  function retryTurn(turn: WorkflowTurn) {
    if (pending) {
      return;
    }
    const index = turns.findIndex((item) => item.id === turn.id);
    const priorTurns = index >= 0 ? turns.slice(0, index) : turns;
    void runTurn(turn.id, turn.question, priorTurns);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      submitQuestion();
    }
  }

  function applySample(prompt: string) {
    setInput(prompt);
    textareaRef.current?.focus();
  }

  function clearConversation() {
    setTurns([]);
    setInput("");
  }

  return (
    <div className="page workflow-page">
      <PageHeader
        eyebrow="Orchestration"
        title="Ask the supervisor"
        description="One request. The graph classifies it, chooses a capability, and returns both the answer and the decision."
        actions={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void loadDocuments()}
              disabled={documentsLoading}
            >
              <RefreshCw size={16} className={documentsLoading ? "spin" : ""} />
              Refresh sources
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={clearConversation}
              disabled={turns.length === 0 || pending}
            >
              <Trash2 size={16} />
              Clear
            </button>
          </>
        }
      />

      <div className="workflow-layout">
        <div className="workflow-main">
          <section className="supervisor-composer-card">
            <div className="supervisor-composer-heading">
              <span className="supervisor-orb"><Network size={20} /></span>
              <div>
                <h2>Route a request</h2>
                <p>
                  The model routes, but each specialist enforces its own
                  grounding and safety rules.
                </p>
              </div>
            </div>

            <form className="supervisor-form" onSubmit={submitQuestion}>
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask about documents, revenue, current events, or request a report…"
                rows={2}
                maxLength={MAX_QUESTION_CHARS}
                disabled={pending}
                aria-label="Supervisor request"
              />
              <div className="supervisor-form-row">
                <label className="field field-inline supervisor-scope">
                  <span>Documents scope</span>
                  <select
                    value={scopeDocumentId}
                    onChange={(event) => setScopeDocumentId(event.target.value)}
                    disabled={pending || readyDocuments.length === 0}
                  >
                    <option value="all">All ready documents</option>
                    {readyDocuments.map((document) => (
                      <option key={document.id} value={document.id}>
                        {document.filename}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="supervisor-submit">
                  <span className="composer-hint">
                    {pending ? "Supervisor is working…" : "Enter to run · Shift+Enter for a new line"}
                  </span>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    disabled={!input.trim() || pending}
                  >
                    {pending ? <Spinner size={16} /> : <Send size={16} />}
                    {pending ? "Running" : "Run"}
                  </button>
                </div>
              </div>
            </form>

            {documentsError !== null && (
              <ErrorNotice
                error={documentsError}
                compact
                title="Document scope could not be loaded"
                onRetry={() => void loadDocuments()}
              />
            )}
          </section>

          {turns.length === 0 ? (
            <section className="panel">
              <EmptyState
                icon={<Waypoints size={25} />}
                title="The supervisor is ready"
                description="Ask a question and it will choose a route. Document and web claims come back with citations; SQL results come back with the query and rows."
                action={
                  <div className="sample-prompt-grid">
                    {SAMPLE_PROMPTS.map((sample) => (
                      <button
                        key={sample.label}
                        type="button"
                        className="sample-prompt"
                        onClick={() => applySample(sample.prompt)}
                      >
                        <span>{sample.label}</span>
                        <strong>{sample.prompt}</strong>
                        <ArrowRight size={15} />
                      </button>
                    ))}
                  </div>
                }
              />
            </section>
          ) : (
            <div className="workflow-turn-list">
              {[...turns].reverse().map((turn) => (
                <article key={turn.id} className="workflow-turn">
                  <header className="workflow-question">
                    <span className="workflow-question-icon">
                      <Sparkles size={17} />
                    </span>
                    <div>
                      <p className="eyebrow">
                        Request · {new Date(turn.startedAt).toLocaleTimeString()}
                      </p>
                      <h2>{turn.question}</h2>
                    </div>
                    {turn.response && (
                      <div className="workflow-route-badges">
                        <RouteBadge route={turn.response.route} />
                        <GroundedBadge grounded={turn.response.grounded} />
                      </div>
                    )}
                  </header>

                  {turn.status === "pending" && (
                    <div className="workflow-pending">
                      <Spinner size={18} />
                      <div>
                        <strong>Classifying and routing…</strong>
                        <span>
                          The supervisor may retrieve, search, query or write.
                          This can take a moment with a local model.
                        </span>
                      </div>
                    </div>
                  )}

                  {turn.status === "error" && (
                    <ErrorNotice
                      error={turn.error}
                      title="The supervisor could not complete this request"
                      onRetry={() => retryTurn(turn)}
                    />
                  )}

                  {turn.status === "success" && turn.response && (
                    <div
                      className={`workflow-answer${
                        highlightAnswer && turn.id === lastSuccessId
                          ? " answer-highlight"
                          : ""
                      }`}
                    >
                      <IntentChips response={turn.response} />
                      <p className="intent-summary">
                        {turn.response.intent.summary}
                      </p>

                      {turn.response.grounded === false && (
                        <div className="inline-alert inline-alert-warning">
                          <AlertTriangle size={16} />
                          <span>
                            The selected source returned no evidence, so the
                            model was not asked to answer. The response states
                            what was missing instead of improvising.
                          </span>
                        </div>
                      )}

                      <Markdown className="workflow-markdown">
                        {turn.response.answer}
                      </Markdown>

                      <div className="workflow-answer-actions">
                        <CopyButton text={turn.response.answer} label="Copy answer" />
                        {turn.response.searched_chunks > 0 && (
                          <span className="muted-inline">
                            {formatNumber(turn.response.searched_chunks)} passages searched
                          </span>
                        )}
                      </div>

                      <EvidenceSections
                        sources={turn.response.sources}
                        webSources={turn.response.web_sources}
                        analytics={turn.response.analytics}
                        searchedChunks={turn.response.searched_chunks}
                      />

                      <ResponseMeta
                        provider={turn.response.provider}
                        model={turn.response.model}
                        usage={turn.response.usage}
                        requestId={turn.response.request_id}
                      />
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>

        <aside className="workflow-rail">
          <section className="rail-card">
            <div className="rail-heading"><Network size={16} /> Routes</div>
            <div className="route-legend">
              {ROUTE_LEGEND.map((item) => {
                const Icon = item.icon;
                const count = routeCounts.get(item.route) ?? 0;
                return (
                  <div key={item.route} className="route-legend-item">
                    <span className="route-legend-icon"><Icon size={15} /></span>
                    <div>
                      <strong>{item.route === "chat" ? "Conversation" : item.route}</strong>
                      <span>{item.description}</span>
                    </div>
                    {count > 0 && <Badge tone="neutral">{count}</Badge>}
                  </div>
                );
              })}
            </div>
          </section>

          <section className="rail-card">
            <div className="rail-heading"><Bot size={16} /> Model</div>
            {llmError !== null ? (
              <p className="muted-note">{errorMessage(llmError)}</p>
            ) : llm ? (
              <dl className="rail-facts">
                <div><dt>Provider</dt><dd>{llm.provider}</dd></div>
                <div><dt>Model</dt><dd className="mono">{llm.model}</dd></div>
                <div><dt>Configured</dt><dd>{llm.configured ? "Yes" : "No"}</dd></div>
              </dl>
            ) : (
              <p className="muted-note">Loading model status…</p>
            )}
          </section>

          <section className="rail-card">
            <div className="rail-heading"><History size={16} /> Conversation</div>
            <dl className="rail-facts">
              <div><dt>Requests</dt><dd>{turns.length}</dd></div>
              <div><dt>History sent</dt><dd>{Math.min(buildHistory(turns).length, 48)} messages</dd></div>
              <div><dt>Scope</dt><dd className="rail-scope">{scopeDocumentId === "all" ? "All ready documents" : "One document"}</dd></div>
            </dl>
            <p className="rail-note">
              Prior successful turns are sent as history for conversational
              continuity. Failed turns do not enter the history.
            </p>
          </section>

          <section className="rail-card rail-card-soft">
            <div className="rail-heading"><AlertTriangle size={16} /> Honest by default</div>
            <p className="rail-note">
              Research requires <code>TAVILY_API_KEY</code>. Without it the
              request fails with a typed 503 rather than returning invented
              sources.
            </p>
          </section>
        </aside>
      </div>
    </div>
  );
}
