/** A typed error for every non-2xx response and network failure. */

export interface ApiErrorOptions {
  details?: Record<string, unknown>;
  requestId?: string;
  retryAfterSeconds?: number;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;
  readonly requestId?: string;
  readonly retryAfterSeconds?: number;

  constructor(
    status: number,
    code: string,
    message: string,
    options: ApiErrorOptions = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = options.details;
    this.requestId = options.requestId;
    this.retryAfterSeconds = options.retryAfterSeconds;
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validationDetail(detail: unknown): string {
  if (!Array.isArray(detail)) {
    return "The request did not match the API's expected shape.";
  }

  const messages = detail
    .slice(0, 5)
    .map((item) => {
      if (!isRecord(item)) {
        return null;
      }
      const location = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== "body").join(".")
        : "";
      const message =
        typeof item.msg === "string" ? item.msg : "Invalid value.";
      return location ? `${location}: ${message}` : message;
    })
    .filter((item): item is string => Boolean(item));

  return messages.length
    ? messages.join("; ")
    : "The request did not match the API's expected shape.";
}

/** Convert a failed fetch Response into an ApiError without losing request ids. */
export async function apiErrorFromResponse(
  response: Response,
): Promise<ApiError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // The API normally returns JSON, but a proxy or network edge may not.
  }

  const headerRequestId =
    response.headers.get("X-Request-ID") ?? undefined;
  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfterSeconds =
    retryAfterHeader && Number.isFinite(Number(retryAfterHeader))
      ? Number(retryAfterHeader)
      : undefined;

  let message = `Request failed with HTTP ${response.status}.`;
  let code = `http_${response.status}`;
  let details: Record<string, unknown> | undefined;
  let requestId = headerRequestId;

  if (isRecord(payload)) {
    const detail = payload.detail;
    if (typeof detail === "string" && detail.trim()) {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = validationDetail(detail);
      code = "validation_error";
    }

    if (typeof payload.code === "string" && payload.code.trim()) {
      code = payload.code;
    }
    if (isRecord(payload.details)) {
      details = payload.details;
    }
    if (typeof payload.request_id === "string") {
      requestId = payload.request_id;
    }
  }

  return new ApiError(response.status, code, message, {
    details,
    requestId,
    retryAfterSeconds,
  });
}

export function retryHint(error: ApiError): string | null {
  if (error.status === 401 || error.code === "not_authenticated") {
    return "Your session may have expired. Sign in again.";
  }
  if (error.status === 429 && error.retryAfterSeconds) {
    return `Try again in about ${error.retryAfterSeconds} second${
      error.retryAfterSeconds === 1 ? "" : "s"
    }.`;
  }
  if (error.code === "web_search_not_configured") {
    return "The backend has no TAVILY_API_KEY, so live web research is unavailable.";
  }
  if (error.status === 0) {
    return "Check that the backend is running and that VITE_API_BASE_URL points to it.";
  }
  return null;
}
