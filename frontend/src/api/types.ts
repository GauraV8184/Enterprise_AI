/**
 * Types mirroring the backend's public Pydantic schemas.
 *
 * Keep these in step with backend/app/schemas/*. They describe the JSON the
 * browser is allowed to see; owner ids, storage paths and credentials are
 * deliberately absent.
 */

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface TokenUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

export type IntentKind =
  | "general_chat"
  | "document_question"
  | "data_analysis"
  | "research"
  | "report";

export interface TaskIntent {
  intent: IntentKind;
  requires_internal_documents: boolean;
  requires_business_data: boolean;
  requires_web_research: boolean;
  complexity: "simple" | "multi_step";
  summary: string;
}

export type WorkflowRoute =
  | "chat"
  | "documents"
  | "research"
  | "analytics"
  | "report";

export interface SourceCitation {
  label: number;
  document_id: string;
  filename: string;
  page: number | null;
  section: string | null;
  score: number;
  snippet: string;
}

export interface WebSource {
  label: string;
  title: string;
  url: string;
  snippet: string;
  score: number | null;
}

export interface AnalyticsEvidence {
  sql: string;
  row_count: number;
  truncated: boolean;
  rows: Array<Record<string, unknown>>;
}

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface DocumentRecord {
  id: string;
  filename: string;
  content_type: string;
  extension: string;
  size_bytes: number;
  status: DocumentStatus;
  error: string | null;
  page_count: number | null;
  chunk_count: number;
  created_at: string;
  indexed_at: string | null;
}

export interface DocumentListResponse {
  documents: DocumentRecord[];
  total: number;
}

export interface HealthResponse {
  status: "ok";
  service: string;
  version: string;
  environment: string;
}

export interface ServiceInfo {
  name: string;
  version: string;
  environment: string;
  docs_url: string;
}

export interface LlmStatus {
  configured: boolean;
  provider: string;
  model: string;
  timeout_seconds: number;
  max_retries: number;
  detail: string | null;
}

export interface ChatResponse {
  answer: string;
  provider: string;
  model: string;
  usage: TokenUsage | null;
  finish_reason: string | null;
  request_id: string;
}

export interface IntentResponse {
  intent: TaskIntent;
  provider: string;
  model: string;
  request_id: string;
}

export interface SearchResponse {
  query: string;
  searched_chunks: number;
  matches: SourceCitation[];
}

export interface AskResponse {
  question: string;
  answer: string;
  grounded: boolean;
  sources: SourceCitation[];
  searched_chunks: number;
  provider: string;
  model: string;
  usage: TokenUsage | null;
  request_id: string;
}

export interface WorkflowResponse {
  answer: string;
  route: WorkflowRoute;
  intent: TaskIntent;
  grounded: boolean | null;
  sources: SourceCitation[];
  searched_chunks: number;
  web_sources: WebSource[];
  analytics: AnalyticsEvidence | null;
  provider: string;
  model: string;
  usage: TokenUsage | null;
  request_id: string;
}

export type ReportSource = "documents" | "analytics" | "web";

export interface ReportResponse {
  report: string;
  topic: string;
  sources_requested: ReportSource[];
  grounded: boolean;
  sources: SourceCitation[];
  web_sources: WebSource[];
  analytics: AnalyticsEvidence | null;
  provider: string;
  model: string;
  usage: TokenUsage | null;
  request_id: string;
}

export interface ChatRequest {
  messages: ChatMessage[];
  temperature?: number;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  document_ids?: string[];
}

export interface AskRequest {
  question: string;
  top_k?: number;
  document_ids?: string[];
}

export interface WorkflowRequest {
  message: string;
  history?: ChatMessage[];
  document_ids?: string[];
}

export interface ReportRequest {
  topic: string;
  sources: ReportSource[];
}
