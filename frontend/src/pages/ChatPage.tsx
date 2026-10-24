import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  AlertTriangle,
  Bot,
  CornerDownLeft,
  MessageSquareText,
  Plus,
  Sparkles,
  Square,
  Trash2,
  UserRound,
} from "lucide-react";

import type {
  ChatMessage,
  ChatResponse,
  LlmStatus,
} from "../api/types";
import { errorMessage, isAbortError } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { CopyButton } from "../components/CopyButton";
import { EmptyState } from "../components/EmptyState";
import { ErrorNotice } from "../components/ErrorNotice";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { Spinner } from "../components/Spinner";
import { useAutoResize } from "../hooks/useAutoResize";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { useTransientFlag } from "../hooks/useTransientFlag";
import { formatNumber } from "../lib/format";

const MAX_MESSAGES = 50;
const MAX_CONTENT_CHARS = 8000;

const STARTERS = [
  "Explain the difference between retrieval-augmented generation and fine-tuning.",
  "Draft a concise executive summary of our AI platform.",
  "What makes a good evidence-backed report?",
];

interface ChatItem {
  id: string;
  role: "user" | "assistant";
  content: string;
  response?: ChatResponse;
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function toChatMessages(items: ChatItem[]): ChatMessage[] {
  return items.map((item) => ({ role: item.role, content: item.content }));
}

export function ChatPage() {
  useDocumentTitle("Chat");
  const { api, user } = useAuth();
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [llmError, setLlmError] = useState<unknown>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useAutoResize(input, 220);

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

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [items, pending]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const totalTokens = useMemo(
    () =>
      items.reduce(
        (total, item) => total + (item.response?.usage?.total_tokens ?? 0),
        0,
      ),
    [items],
  );

  const lastAssistantId = useMemo(() => {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (items[index].role === "assistant") {
        return items[index].id;
      }
    }
    return null;
  }, [items]);
  const highlightAnswer = useTransientFlag(pending ? null : lastAssistantId);

  const limitReached = items.length >= MAX_MESSAGES;

  const complete = useCallback(
    async (history: ChatItem[]) => {
      if (history.length === 0 || history.length > MAX_MESSAGES) {
        return;
      }
      const controller = new AbortController();
      abortRef.current = controller;
      setPending(true);
      setError(null);
      setNotice(null);

      try {
        const response = await api.chat(
          { messages: toChatMessages(history) },
          controller.signal,
        );
        setItems((current) => [
          ...current,
          {
            id: makeId(),
            role: "assistant",
            content: response.answer,
            response,
          },
        ]);
      } catch (caught) {
        if (isAbortError(caught)) {
          setNotice("Request cancelled.");
        } else {
          setError(caught);
        }
      } finally {
        abortRef.current = null;
        setPending(false);
      }
    },
    [api],
  );

  function send(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const content = input.trim();
    if (!content || pending || limitReached) {
      return;
    }

    const next = [
      ...items,
      { id: makeId(), role: "user" as const, content },
    ];
    setItems(next);
    setInput("");
    void complete(next);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      send();
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  function clear() {
    cancel();
    setItems([]);
    setError(null);
    setNotice(null);
    setInput("");
  }

  function retry() {
    void complete(items);
  }

  function applyStarter(starter: string) {
    setInput(starter);
    textareaRef.current?.focus();
  }

  const userInitial = user?.name.charAt(0).toUpperCase() ?? "U";

  return (
    <div className="page chat-page">
      <PageHeader
        eyebrow="Conversation"
        title="Chat with the model"
        description="A direct, ungrounded conversation surface. For answers tied to sources, use Ask supervisor or Documents."
        actions={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={clear}
              disabled={items.length === 0 && !pending}
            >
              <Trash2 size={16} />
              Clear
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={clear}
              disabled={pending}
            >
              <Plus size={17} />
              New chat
            </button>
          </>
        }
      />

      <div className="chat-workspace">
        <section className="chat-card" aria-label="Chat conversation">
          <header className="chat-card-header">
            <div className="chat-model">
              <span className="chat-model-icon"><Bot size={18} /></span>
              <div>
                <strong>{llm?.model ?? "Assistant"}</strong>
                <span>
                  {llm?.configured
                    ? `${llm.provider} · direct model call`
                    : "Model status unavailable"}
                </span>
              </div>
            </div>
            <div className="chat-header-meta">
              <span>{items.length}/{MAX_MESSAGES} messages</span>
              {pending && (
                <span className="chat-thinking">
                  <span className="typing-dots" aria-hidden="true">
                    <i /><i /><i />
                  </span>
                  Thinking
                </span>
              )}
            </div>
          </header>

          <div className="chat-messages">
            {items.length === 0 && !pending ? (
              <div className="chat-empty">
                <EmptyState
                  icon={<Sparkles size={25} />}
                  title="Start a conversation"
                  description="Ask for an explanation, a draft, or a second opinion. This surface answers from the model alone; it does not retrieve your documents."
                  action={
                    <div className="starter-list">
                      {STARTERS.map((starter) => (
                        <button
                          key={starter}
                          type="button"
                          className="starter-button"
                          onClick={() => applyStarter(starter)}
                        >
                          <MessageSquareText size={15} />
                          <span>{starter}</span>
                        </button>
                      ))}
                    </div>
                  }
                />
              </div>
            ) : (
              <div className="message-list">
                {items.map((item) => (
                  <article
                    key={item.id}
                    className={`message message-${item.role}`}
                  >
                    <span className="message-avatar" aria-hidden="true">
                      {item.role === "user"
                        ? userInitial
                        : <Bot size={18} />}
                    </span>
                    <div
                      className={`message-content${
                        highlightAnswer && item.id === lastAssistantId
                          ? " answer-highlight"
                          : ""
                      }`}
                    >
                      <div className="message-role">
                        {item.role === "user" ? "You" : "Assistant"}
                        {item.response && (
                          <span className="message-model">
                            {item.response.model}
                          </span>
                        )}
                      </div>
                      {item.role === "assistant" ? (
                        <Markdown>{item.content}</Markdown>
                      ) : (
                        <p className="user-text">{item.content}</p>
                      )}
                      {item.role === "assistant" && item.response && (
                        <details className="message-details">
                          <summary>Response details</summary>
                          <div className="message-details-grid">
                            <span>
                              <strong>Provider</strong>
                              {item.response.provider}
                            </span>
                            <span>
                              <strong>Finish</strong>
                              {item.response.finish_reason ?? "n/a"}
                            </span>
                            <span>
                              <strong>Tokens</strong>
                              {item.response.usage
                                ? `${formatNumber(item.response.usage.input_tokens ?? 0)} in / ${formatNumber(item.response.usage.output_tokens ?? 0)} out`
                                : "not reported"}
                            </span>
                            <span>
                              <strong>Request</strong>
                              <code>{item.response.request_id}</code>
                            </span>
                          </div>
                        </details>
                      )}
                      {item.role === "assistant" && (
                        <div className="message-actions">
                          <CopyButton text={item.content} label="Copy answer" />
                        </div>
                      )}
                    </div>
                  </article>
                ))}

                {pending && (
                  <article className="message message-assistant">
                    <span className="message-avatar" aria-hidden="true">
                      <Bot size={18} />
                    </span>
                    <div className="message-content">
                      <div className="message-role">Assistant</div>
                      <div className="assistant-pending">
                        <Spinner size={16} label="Waiting for the model" />
                        <span>Generating a response…</span>
                      </div>
                    </div>
                  </article>
                )}
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <div className="chat-composer-area">
            {error !== null && (
              <ErrorNotice
                error={error}
                title="The model request failed"
                onRetry={retry}
                compact
              />
            )}
            {notice && (
              <div className="inline-alert inline-alert-neutral" role="status">
                {notice}
              </div>
            )}
            {limitReached && (
              <div className="inline-alert inline-alert-warning" role="status">
                <AlertTriangle size={16} />
                <span>
                  This conversation reached the API&apos;s {MAX_MESSAGES}-message
                  limit. Clear or start a new chat to continue.
                </span>
              </div>
            )}

            <form className="composer" onSubmit={send}>
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  limitReached
                    ? "Message limit reached — clear the conversation to continue."
                    : "Write a message…"
                }
                rows={1}
                maxLength={MAX_CONTENT_CHARS}
                disabled={pending || limitReached}
                aria-label="Chat message"
              />
              <div className="composer-toolbar">
                <span className="composer-hint">
                  {input.length > 7600
                    ? `${formatNumber(input.length)} / ${formatNumber(MAX_CONTENT_CHARS)}`
                    : "Enter to send · Shift+Enter for a new line"}
                </span>
                {pending ? (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={cancel}
                  >
                    <Square size={14} />
                    Stop
                  </button>
                ) : (
                  <button
                    type="submit"
                    className="btn btn-primary btn-sm"
                    disabled={!input.trim() || limitReached}
                  >
                    Send
                    <CornerDownLeft size={15} />
                  </button>
                )}
              </div>
            </form>
            <p className="composer-footnote">
              <UserRound size={13} />
              Messages stay in this browser tab and are not persisted.
            </p>
          </div>
        </section>

        <aside className="chat-rail" aria-label="Chat information">
          <section className="rail-card">
            <div className="rail-heading">
              <Bot size={16} />
              Model
            </div>
            {llmError !== null ? (
              <p className="muted-note">{errorMessage(llmError)}</p>
            ) : llm ? (
              <>
                <div className="rail-status">
                  <span
                    className={`status-dot ${llm.configured ? "status-dot-on" : "status-dot-off"}`}
                  />
                  {llm.configured ? "Configured" : "Not configured"}
                </div>
                <dl className="rail-facts">
                  <div><dt>Provider</dt><dd>{llm.provider}</dd></div>
                  <div><dt>Model</dt><dd className="mono">{llm.model}</dd></div>
                  <div><dt>Timeout</dt><dd>{llm.timeout_seconds}s</dd></div>
                  <div><dt>Retries</dt><dd>{llm.max_retries}</dd></div>
                </dl>
                {!llm.configured && llm.detail && (
                  <p className="muted-note">{llm.detail}</p>
                )}
              </>
            ) : (
              <p className="muted-note">Loading model status…</p>
            )}
          </section>

          <section className="rail-card">
            <div className="rail-heading">
              <MessageSquareText size={16} />
              Session
            </div>
            <dl className="rail-facts">
              <div><dt>Messages</dt><dd>{items.length}</dd></div>
              <div><dt>Total tokens</dt><dd>{totalTokens ? formatNumber(totalTokens) : "—"}</dd></div>
              <div><dt>Persistence</dt><dd>None</dd></div>
            </dl>
            <p className="rail-note">
              For cited answers from your own files, switch to Documents →
              Ask. For multi-capability answers, use Ask supervisor.
            </p>
          </section>

          <section className="rail-card rail-card-soft">
            <div className="rail-heading">
              <Sparkles size={16} />
              Prompting tip
            </div>
            <p className="rail-note">
              State the audience and desired format: &ldquo;Draft a 150-word
              summary for the executive team.&rdquo;
            </p>
          </section>
        </aside>
      </div>
    </div>
  );
}
