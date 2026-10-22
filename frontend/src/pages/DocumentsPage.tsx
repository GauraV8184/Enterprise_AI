import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Database,
  FileQuestion,
  Files,
  Filter,
  Loader2,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";

import type {
  AskResponse,
  DocumentRecord,
  SearchResponse,
} from "../api/types";
import { errorMessage, isAbortError } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import { Badge } from "../components/Badge";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { CopyButton } from "../components/CopyButton";
import { DocumentStatusBadge, GroundedBadge } from "../components/StatusBadges";
import { EmptyState } from "../components/EmptyState";
import { ErrorNotice } from "../components/ErrorNotice";
import { EvidenceSections, ResponseMeta } from "../components/EvidencePanel";
import { Markdown } from "../components/Markdown";
import { PageHeader } from "../components/PageHeader";
import { Spinner } from "../components/Spinner";
import { useDocumentTitle } from "../hooks/useDocumentTitle";
import { formatBytes, formatDateTime, formatNumber, formatRelativeTime } from "../lib/format";
import { UPLOAD_ACCEPT } from "../lib/config";
import { validateFile } from "../lib/files";

type PageTab = "library" | "ask";
type RetrievalMode = "ask" | "search";
type UploadStatus = "queued" | "uploading" | "done" | "error";

interface UploadItem {
  id: string;
  name: string;
  size: number;
  status: UploadStatus;
  error?: string;
}

interface PendingUpload {
  id: string;
  file: File;
}

function makeUploadId(): string {
  return `upload-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function extensionLabel(extension: string): string {
  return extension.replace(/^\./, "").toUpperCase() || "FILE";
}

export function DocumentsPage() {
  useDocumentTitle("Documents");
  const { api } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const initialDocumentId = searchParams.get("document");
  const initialTab: PageTab =
    searchParams.get("mode") === "ask" || initialDocumentId
      ? "ask"
      : "library";

  const [tab, setTab] = useState<PageTab>(initialTab);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [documentsLoading, setDocumentsLoading] = useState(true);
  const [documentsError, setDocumentsError] = useState<unknown>(null);
  const [filter, setFilter] = useState("");

  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [deleting, setDeleting] = useState<DocumentRecord | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);

  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>("ask");
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState("6");
  const [scopeDocumentId, setScopeDocumentId] = useState(
    initialDocumentId ?? "all",
  );
  const [retrievalBusy, setRetrievalBusy] = useState(false);
  const [retrievalError, setRetrievalError] = useState<unknown>(null);
  const [retrievalNotice, setRetrievalNotice] = useState<string | null>(null);
  const [askResult, setAskResult] = useState<AskResponse | null>(null);
  const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadQueueRef = useRef<PendingUpload[]>([]);
  const uploadProcessingRef = useRef(false);
  const retrievalAbortRef = useRef<AbortController | null>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);

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
  }, [loadDocuments]);

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

  useEffect(() => {
    return () => retrievalAbortRef.current?.abort();
  }, []);

  const readyDocuments = useMemo(
    () => documents.filter((document) => document.status === "ready"),
    [documents],
  );

  const stats = useMemo(
    () => ({
      total: documents.length,
      ready: readyDocuments.length,
      failed: documents.filter((document) => document.status === "failed").length,
      chunks: documents.reduce((total, document) => total + document.chunk_count, 0),
    }),
    [documents, readyDocuments.length],
  );

  const filteredDocuments = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) {
      return documents;
    }
    return documents.filter((document) =>
      document.filename.toLowerCase().includes(needle),
    );
  }, [documents, filter]);

  function patchUpload(id: string, patch: Partial<UploadItem>) {
    setUploads((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  const drainUploads = useCallback(async () => {
    if (uploadProcessingRef.current) {
      return;
    }
    uploadProcessingRef.current = true;

    try {
      while (uploadQueueRef.current.length > 0) {
        const next = uploadQueueRef.current.shift();
        if (!next) {
          break;
        }
        patchUpload(next.id, { status: "uploading", error: undefined });
        try {
          await api.uploadDocument(next.file);
          patchUpload(next.id, { status: "done" });
        } catch (caught) {
          patchUpload(next.id, { status: "error", error: errorMessage(caught) });
        }
      }
    } finally {
      uploadProcessingRef.current = false;
      await loadDocuments();
    }
  }, [api, loadDocuments]);

  function enqueueFiles(files: File[]) {
    if (files.length === 0) {
      return;
    }

    const items: UploadItem[] = [];
    const pending: PendingUpload[] = [];

    for (const file of files) {
      const id = makeUploadId();
      const validation = validateFile(file);
      if (validation.valid) {
        items.push({
          id,
          name: file.name,
          size: file.size,
          status: "queued",
        });
        pending.push({ id, file });
      } else {
        items.push({
          id,
          name: file.name,
          size: file.size,
          status: "error",
          error: validation.reason ?? "This file cannot be uploaded.",
        });
      }
    }

    setUploads((current) => [...items, ...current]);
    uploadQueueRef.current.push(...pending);
    void drainUploads();
  }

  function handleFileInput(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    enqueueFiles(files);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragActive(false);
    enqueueFiles(Array.from(event.dataTransfer.files));
  }

  function switchTab(next: PageTab) {
    setTab(next);
    setDeleteError(null);
  }

  function askAboutDocument(document: DocumentRecord) {
    setScopeDocumentId(document.id);
    setRetrievalMode("ask");
    setTab("ask");
    setQuestion("");
    setAskResult(null);
    setSearchResult(null);
    setRetrievalError(null);
    window.setTimeout(() => questionRef.current?.focus(), 0);
  }

  async function confirmDelete() {
    if (!deleting) {
      return;
    }
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteDocument(deleting.id);
      if (scopeDocumentId === deleting.id) {
        setScopeDocumentId("all");
      }
      setDeleting(null);
      await loadDocuments();
    } catch (caught) {
      setDeleteError(caught);
    } finally {
      setDeleteBusy(false);
    }
  }

  async function runRetrieval(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const value = question.trim();
    if (!value || retrievalBusy || readyDocuments.length === 0) {
      return;
    }

    const controller = new AbortController();
    retrievalAbortRef.current = controller;
    setRetrievalBusy(true);
    setRetrievalError(null);
    setRetrievalNotice(null);
    setAskResult(null);
    setSearchResult(null);

    const documentIds =
      scopeDocumentId === "all" ? undefined : [scopeDocumentId];
    const parsedTopK = Number(topK);

    try {
      if (retrievalMode === "ask") {
        const response = await api.askDocuments(
          {
            question: value,
            top_k: parsedTopK,
            document_ids: documentIds,
          },
          controller.signal,
        );
        setAskResult(response);
      } else {
        const response = await api.searchDocuments(
          {
            query: value,
            top_k: parsedTopK,
            document_ids: documentIds,
          },
          controller.signal,
        );
        setSearchResult(response);
      }
    } catch (caught) {
      if (isAbortError(caught)) {
        setRetrievalNotice("Request cancelled.");
      } else {
        setRetrievalError(caught);
      }
    } finally {
      retrievalAbortRef.current = null;
      setRetrievalBusy(false);
    }
  }

  function cancelRetrieval() {
    retrievalAbortRef.current?.abort();
  }

  function changeRetrievalMode(mode: RetrievalMode) {
    setRetrievalMode(mode);
    setAskResult(null);
    setSearchResult(null);
    setRetrievalError(null);
    setRetrievalNotice(null);
  }

  const selectedScopeName =
    scopeDocumentId === "all"
      ? "all ready documents"
      : documents.find((document) => document.id === scopeDocumentId)?.filename ??
        "the selected document";

  return (
    <div className="page documents-page">
      <PageHeader
        eyebrow="Knowledge base"
        title="Documents"
        description="Upload your sources, inspect indexing status, and ask questions grounded in the passages that were found."
        actions={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void loadDocuments()}
              disabled={documentsLoading}
            >
              <RefreshCw size={16} className={documentsLoading ? "spin" : ""} />
              Refresh
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setTab("library");
                fileInputRef.current?.click();
              }}
            >
              <UploadCloud size={17} />
              Upload
            </button>
          </>
        }
      />

      <div className="doc-tabs-row">
        <div className="segmented" role="tablist" aria-label="Documents workspace">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "library"}
            className={tab === "library" ? "active" : ""}
            onClick={() => switchTab("library")}
          >
            <Files size={16} />
            Library
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "ask"}
            className={tab === "ask" ? "active" : ""}
            onClick={() => switchTab("ask")}
          >
            <Sparkles size={16} />
            Ask &amp; search
          </button>
        </div>
        <div className="doc-summary">
          <span><strong>{formatNumber(stats.total)}</strong> documents</span>
          <span><strong>{formatNumber(stats.chunks)}</strong> chunks</span>
          {stats.failed > 0 && (
            <Badge tone="danger">{stats.failed} failed</Badge>
          )}
        </div>
      </div>

      {tab === "library" ? (
        <>
          <input
            ref={fileInputRef}
            type="file"
            accept={UPLOAD_ACCEPT}
            multiple
            className="file-input-hidden"
            onChange={handleFileInput}
          />

          <div
            className={`upload-zone ${dragActive ? "is-dragging" : ""}`.trim()}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (event.currentTarget === event.target) {
                setDragActive(false);
              }
            }}
            onDrop={handleDrop}
          >
            <span className="upload-icon"><UploadCloud size={26} /></span>
            <div className="upload-copy">
              <h2>Drop files here, or choose from your computer</h2>
              <p>
                PDF, DOCX, XLSX, CSV, TXT and Markdown. Indexing runs inline, so
                a ready file is already searchable.
              </p>
            </div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => fileInputRef.current?.click()}
            >
              Browse files
            </button>
          </div>

          {uploads.length > 0 && (
            <section className="upload-activity" aria-label="Upload activity">
              <div className="upload-activity-heading">
                <strong>Upload activity</strong>
                <button
                  type="button"
                  className="text-button"
                  onClick={() =>
                    setUploads((current) =>
                      current.filter(
                        (item) =>
                          item.status === "queued" ||
                          item.status === "uploading",
                      ),
                    )
                  }
                >
                  Clear finished
                </button>
              </div>
              <div className="upload-activity-list">
                {uploads.map((upload) => (
                  <div key={upload.id} className={`upload-item upload-${upload.status}`}>
                    <span className="upload-item-icon">
                      {upload.status === "uploading" ? (
                        <Loader2 size={17} className="spin" />
                      ) : upload.status === "done" ? (
                        <CheckCircle2 size={17} />
                      ) : upload.status === "error" ? (
                        <AlertTriangle size={17} />
                      ) : (
                        <FileQuestion size={17} />
                      )}
                    </span>
                    <span className="upload-item-copy">
                      <strong>{upload.name}</strong>
                      <small>
                        {formatBytes(upload.size)}
                        {upload.status === "queued" && " · waiting"}
                        {upload.status === "uploading" && " · uploading and indexing"}
                        {upload.status === "done" && " · indexed"}
                        {upload.status === "error" && ` · ${upload.error ?? "failed"}`}
                      </small>
                    </span>
                    {upload.status !== "uploading" && upload.status !== "queued" && (
                      <button
                        type="button"
                        className="icon-button"
                        onClick={() =>
                          setUploads((current) =>
                            current.filter((item) => item.id !== upload.id),
                          )
                        }
                        aria-label={`Dismiss ${upload.name}`}
                      >
                        <X size={15} />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {documentsError !== null && (
            <ErrorNotice
              error={documentsError}
              title="The library could not be loaded"
              onRetry={() => void loadDocuments()}
            />
          )}

          <section className="panel document-library">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><Files size={18} /></span>
                <div>
                  <h2>Your library</h2>
                  <p>Every file is scoped to your identity by Row Level Security.</p>
                </div>
              </div>
              <div className="library-tools">
                <label className="filter-box">
                  <Filter size={15} />
                  <input
                    type="search"
                    placeholder="Filter by filename"
                    value={filter}
                    onChange={(event) => setFilter(event.target.value)}
                    aria-label="Filter documents by filename"
                  />
                </label>
              </div>
            </header>

            <div className="document-list">
              {documentsLoading ? (
                <div className="loading-row">
                  <Spinner size={18} />
                  <span>Loading your documents…</span>
                </div>
              ) : filteredDocuments.length === 0 ? (
                <EmptyState
                  icon={<UploadCloud size={25} />}
                  title={documents.length === 0 ? "Your library is empty" : "No documents match that filter"}
                  description={
                    documents.length === 0
                      ? "Upload a PDF, DOCX, XLSX, CSV, TXT or Markdown file. The backend extracts, chunks and embeds it before returning."
                      : "Try a shorter filename search."
                  }
                  action={
                    documents.length === 0 ? (
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => fileInputRef.current?.click()}
                      >
                        <UploadCloud size={16} />
                        Upload your first document
                      </button>
                    ) : undefined
                  }
                />
              ) : (
                <div className="document-table-wrap">
                  <table className="document-table">
                    <thead>
                      <tr>
                        <th>File</th>
                        <th>Status</th>
                        <th>Size</th>
                        <th>Pages / chunks</th>
                        <th>Added</th>
                        <th><span className="sr-only">Actions</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredDocuments.map((document) => (
                        <tr key={document.id}>
                          <td>
                            <div className="document-name-cell">
                              <span className={`file-type file-type-${document.extension.replace(".", "")}`}>
                                {extensionLabel(document.extension)}
                              </span>
                              <div>
                                <strong title={document.filename}>{document.filename}</strong>
                                {document.status === "failed" && document.error && (
                                  <small className="document-error">{document.error}</small>
                                )}
                              </div>
                            </div>
                          </td>
                          <td>
                            <DocumentStatusBadge status={document.status} />
                          </td>
                          <td className="mono">{formatBytes(document.size_bytes)}</td>
                          <td>
                            <span className="document-counts">
                              {document.page_count !== null
                                ? `${formatNumber(document.page_count)} page${document.page_count === 1 ? "" : "s"}`
                                : "—"}
                              <span className="muted-sep">·</span>
                              {formatNumber(document.chunk_count)} chunk
                              {document.chunk_count === 1 ? "" : "s"}
                            </span>
                          </td>
                          <td title={formatDateTime(document.created_at)}>
                            {formatRelativeTime(document.created_at)}
                          </td>
                          <td>
                            <div className="row-actions">
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                disabled={document.status !== "ready"}
                                onClick={() => askAboutDocument(document)}
                              >
                                <Search size={14} />
                                Ask
                              </button>
                              <button
                                type="button"
                                className="icon-button icon-button-danger"
                                onClick={() => {
                                  setDeleteError(null);
                                  setDeleting(document);
                                }}
                                aria-label={`Delete ${document.filename}`}
                                title="Delete document"
                              >
                                <Trash2 size={15} />
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        </>
      ) : (
        <div className="retrieval-layout">
          <section className="panel retrieval-panel">
            <header className="panel-header">
              <div className="panel-title-wrap">
                <span className="panel-icon"><Sparkles size={18} /></span>
                <div>
                  <h2>{retrievalMode === "ask" ? "Ask your documents" : "Semantic search"}</h2>
                  <p>
                    {retrievalMode === "ask"
                      ? "Answers are written only from retrieved passages and carry citations."
                      : "Inspect retrieval on its own. No model writes text for this result."}
                  </p>
                </div>
              </div>
            </header>

            <div className="retrieval-mode-row">
              <div className="segmented segmented-sm" role="tablist" aria-label="Retrieval mode">
                <button
                  type="button"
                  role="tab"
                  aria-selected={retrievalMode === "ask"}
                  className={retrievalMode === "ask" ? "active" : ""}
                  onClick={() => changeRetrievalMode("ask")}
                >
                  Ask
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={retrievalMode === "search"}
                  className={retrievalMode === "search" ? "active" : ""}
                  onClick={() => changeRetrievalMode("search")}
                >
                  Search
                </button>
              </div>
              {readyDocuments.length === 0 && (
                <Badge tone="warning" icon={<AlertTriangle size={13} />}>
                  No ready documents
                </Badge>
              )}
            </div>

            <form className="retrieval-form" onSubmit={runRetrieval}>
              <label className="field">
                <span>
                  {retrievalMode === "ask" ? "Question" : "Query"}
                </span>
                <textarea
                  ref={questionRef}
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  placeholder={
                    retrievalMode === "ask"
                      ? "What does our policy say about annual leave?"
                      : "annual leave entitlement"
                  }
                  rows={3}
                  maxLength={retrievalMode === "ask" ? 4000 : 2000}
                  disabled={retrievalBusy || readyDocuments.length === 0}
                />
              </label>

              <div className="retrieval-controls">
                <label className="field field-inline">
                  <span>Scope</span>
                  <select
                    value={scopeDocumentId}
                    onChange={(event) => setScopeDocumentId(event.target.value)}
                    disabled={retrievalBusy || readyDocuments.length === 0}
                  >
                    <option value="all">All ready documents</option>
                    {readyDocuments.map((document) => (
                      <option key={document.id} value={document.id}>
                        {document.filename}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field field-inline field-narrow">
                  <span>Passages</span>
                  <select
                    value={topK}
                    onChange={(event) => setTopK(event.target.value)}
                    disabled={retrievalBusy}
                  >
                    {[3, 6, 10, 15].map((value) => (
                      <option key={value} value={value}>{value}</option>
                    ))}
                  </select>
                </label>
                <div className="retrieval-submit">
                  {retrievalBusy ? (
                    <button type="button" className="btn btn-secondary" onClick={cancelRetrieval}>
                      <X size={16} />
                      Cancel
                    </button>
                  ) : (
                    <button
                      type="submit"
                      className="btn btn-primary"
                      disabled={!question.trim() || readyDocuments.length === 0}
                    >
                      {retrievalMode === "ask" ? <Send size={16} /> : <Search size={16} />}
                      {retrievalMode === "ask" ? "Ask documents" : "Search"}
                    </button>
                  )}
                </div>
              </div>
            </form>

            {readyDocuments.length === 0 && (
              <div className="inline-alert inline-alert-warning" role="status">
                <AlertTriangle size={16} />
                <span>
                  Upload and index at least one document before asking. The
                  backend never invents an answer from an empty corpus.
                </span>
              </div>
            )}
            {retrievalError !== null && (
              <ErrorNotice
                error={retrievalError}
                title="The document request failed"
                onRetry={() => void runRetrieval()}
                compact
              />
            )}
            {retrievalNotice && (
              <div className="inline-alert inline-alert-neutral" role="status">
                {retrievalNotice}
              </div>
            )}

            {retrievalBusy && (
              <div className="retrieval-loading">
                <Spinner size={18} />
                <span>
                  {retrievalMode === "ask"
                    ? "Retrieving passages and asking the model…"
                    : "Comparing your query with indexed passages…"}
                </span>
              </div>
            )}

            {askResult && (
              <div className="answer-result">
                <div className="answer-heading">
                  <div>
                    <p className="eyebrow">Grounded answer</p>
                    <h3>{askResult.question}</h3>
                  </div>
                  <GroundedBadge grounded={askResult.grounded} />
                </div>
                {!askResult.grounded && (
                  <div className="inline-alert inline-alert-warning">
                    <AlertTriangle size={16} />
                    <span>
                      No relevant passages were found, so the model was not
                      called. Try a more specific question or upload a source.
                    </span>
                  </div>
                )}
                <Markdown className="answer-markdown">{askResult.answer}</Markdown>
                <div className="answer-actions">
                  <CopyButton text={askResult.answer} label="Copy answer" />
                  <span className="answer-scope">
                    {formatNumber(askResult.searched_chunks)} passage
                    {askResult.searched_chunks === 1 ? "" : "s"} compared ·{" "}
                    {selectedScopeName}
                  </span>
                </div>
                <EvidenceSections
                  sources={askResult.sources}
                  searchedChunks={askResult.searched_chunks}
                />
                <ResponseMeta
                  provider={askResult.provider}
                  model={askResult.model}
                  usage={askResult.usage}
                  requestId={askResult.request_id}
                />
              </div>
            )}

            {searchResult && (
              <div className="answer-result">
                <div className="answer-heading">
                  <div>
                    <p className="eyebrow">Retrieval result</p>
                    <h3>&ldquo;{searchResult.query}&rdquo;</h3>
                  </div>
                  <Badge tone={searchResult.matches.length ? "info" : "warning"}>
                    {searchResult.matches.length} match
                    {searchResult.matches.length === 1 ? "" : "es"}
                  </Badge>
                </div>
                <p className="retrieval-summary">
                  Compared {formatNumber(searchResult.searched_chunks)} indexed
                  passage{searchResult.searched_chunks === 1 ? "" : "s"} in{" "}
                  {selectedScopeName}. No model wrote any text for this result.
                </p>
                {searchResult.matches.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<Search size={22} />}
                    title="Nothing crossed the relevance floor"
                    description="Try a different phrasing, widen the scope, or check that the relevant document is indexed."
                  />
                ) : (
                  <EvidenceSections
                    sources={searchResult.matches}
                    searchedChunks={searchResult.searched_chunks}
                  />
                )}
              </div>
            )}
          </section>

          <aside className="retrieval-rail">
            <section className="rail-card">
              <div className="rail-heading"><Database size={16} /> Corpus</div>
              <dl className="rail-facts">
                <div><dt>Ready documents</dt><dd>{readyDocuments.length}</dd></div>
                <div><dt>Searchable chunks</dt><dd>{formatNumber(stats.chunks)}</dd></div>
                <div><dt>Current scope</dt><dd className="rail-scope">{selectedScopeName}</dd></div>
              </dl>
              <button
                type="button"
                className="btn btn-secondary btn-block btn-sm"
                onClick={() => switchTab("library")}
              >
                <Files size={15} />
                Manage library
              </button>
            </section>

            <section className="rail-card rail-card-soft">
              <div className="rail-heading"><Sparkles size={16} /> How it works</div>
              <ol className="rail-steps">
                <li>Your question is embedded with the configured embedding model.</li>
                <li>Cosine similarity ranks your indexed chunks.</li>
                <li>Only passages above the relevance floor are shown to the model.</li>
                <li>Every citation resolves to a file and page or section.</li>
              </ol>
            </section>

            <section className="rail-card">
              <div className="rail-heading"><ArrowRight size={16} /> Need routing?</div>
              <p className="rail-note">
                Use <strong>Ask supervisor</strong> when the request may need
                analytics, web research or a report instead of documents alone.
              </p>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => navigate("/ask")}
              >
                Open supervisor
              </button>
            </section>
          </aside>
        </div>
      )}

      {deleteError !== null && (
        <ErrorNotice
          error={deleteError}
          title="The document could not be deleted"
          onRetry={() => void confirmDelete()}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="Delete this document?"
        description={
          deleting
            ? `This removes ${deleting.filename}, every vector derived from it, and its stored original. It cannot be undone.`
            : ""
        }
        confirmLabel="Delete document"
        busy={deleteBusy}
        onCancel={() => {
          if (!deleteBusy) {
            setDeleting(null);
          }
        }}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
