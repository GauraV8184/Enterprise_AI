/**
 * Small typed fetch wrapper.
 *
 * The browser never receives privileged keys. Supabase Auth contributes a
 * short-lived bearer token; the development mode contributes only X-User-Id.
 */

import { API_BASE_URL } from "../lib/config";
import { ApiError, apiErrorFromResponse, isAbortError } from "./errors";

export type AuthHeadersProvider = () => Promise<Record<string, string>>;

export interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  json?: unknown;
  formData?: FormData;
  auth?: boolean;
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  options: RequestOptions,
  getAuthHeaders: AuthHeadersProvider,
): Promise<T> {
  const { method = "GET", json, formData, auth = true, signal } = options;

  const headers: Record<string, string> = {
    Accept: "application/json",
  };

  if (auth) {
    Object.assign(headers, await getAuthHeaders());
  }

  let body: BodyInit | undefined;
  if (formData) {
    body = formData;
  } else if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body,
      signal,
      credentials: "omit",
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ApiError(
      0,
      "network_error",
      `Could not reach the API at ${API_BASE_URL}. Is the backend running?`,
      { cause: error },
    );
  }

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export interface ApiClient {
  getHealth(signal?: AbortSignal): Promise<import("./types").HealthResponse>;
  getLlmStatus(signal?: AbortSignal): Promise<import("./types").LlmStatus>;
  listDocuments(
    signal?: AbortSignal,
  ): Promise<import("./types").DocumentListResponse>;
  uploadDocument(
    file: File,
    signal?: AbortSignal,
  ): Promise<import("./types").DocumentRecord>;
  deleteDocument(documentId: string, signal?: AbortSignal): Promise<void>;
  searchDocuments(
    input: import("./types").SearchRequest,
    signal?: AbortSignal,
  ): Promise<import("./types").SearchResponse>;
  askDocuments(
    input: import("./types").AskRequest,
    signal?: AbortSignal,
  ): Promise<import("./types").AskResponse>;
  chat(
    input: import("./types").ChatRequest,
    signal?: AbortSignal,
  ): Promise<import("./types").ChatResponse>;
  runWorkflow(
    input: import("./types").WorkflowRequest,
    signal?: AbortSignal,
  ): Promise<import("./types").WorkflowResponse>;
  generateReport(
    input: import("./types").ReportRequest,
    signal?: AbortSignal,
  ): Promise<import("./types").ReportResponse>;
}

export function createApiClient(
  getAuthHeaders: AuthHeadersProvider,
): ApiClient {
  return {
    getHealth: (signal) =>
      request("/health", { auth: false, signal }, getAuthHeaders),

    getLlmStatus: (signal) =>
      request("/chat/status", { signal }, getAuthHeaders),

    listDocuments: (signal) =>
      request("/documents", { signal }, getAuthHeaders),

    uploadDocument: (file, signal) => {
      const formData = new FormData();
      formData.append("file", file, file.name);
      return request("/documents", { method: "POST", formData, signal }, getAuthHeaders);
    },

    deleteDocument: (documentId, signal) =>
      request(`/documents/${encodeURIComponent(documentId)}`, {
        method: "DELETE",
        signal,
      }, getAuthHeaders),

    searchDocuments: (input, signal) =>
      request("/documents/search", {
        method: "POST",
        json: input,
        signal,
      }, getAuthHeaders),

    askDocuments: (input, signal) =>
      request("/documents/ask", {
        method: "POST",
        json: input,
        signal,
      }, getAuthHeaders),

    chat: (input, signal) =>
      request("/chat", { method: "POST", json: input, signal }, getAuthHeaders),

    runWorkflow: (input, signal) =>
      request("/workflow/run", {
        method: "POST",
        json: input,
        signal,
      }, getAuthHeaders),

    generateReport: (input, signal) =>
      request("/reports", {
        method: "POST",
        json: input,
        signal,
      }, getAuthHeaders),
  };
}
