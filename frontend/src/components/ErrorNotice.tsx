import { AlertTriangle, RotateCw } from "lucide-react";

import { isApiError, retryHint } from "../api/errors";
import { errorMessage } from "../api/errors";

interface ErrorNoticeProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  retryLabel?: string;
  compact?: boolean;
}

export function ErrorNotice({
  error,
  title = "That request did not complete",
  onRetry,
  retryLabel = "Try again",
  compact = false,
}: ErrorNoticeProps) {
  const apiError = isApiError(error) ? error : null;
  const hint = apiError ? retryHint(apiError) : null;

  return (
    <div
      className={`error-notice ${compact ? "error-notice-compact" : ""}`.trim()}
      role="alert"
    >
      <span className="error-notice-icon" aria-hidden="true">
        <AlertTriangle size={19} />
      </span>
      <div className="error-notice-body">
        <div className="error-notice-title">
          {title}
          {apiError && <code className="error-code">{apiError.code}</code>}
        </div>
        <p>{errorMessage(error)}</p>
        {hint && <p className="error-hint">{hint}</p>}
        {apiError?.requestId && (
          <p className="error-request">
            Request <code>{apiError.requestId}</code>
          </p>
        )}
      </div>
      {onRetry && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
          <RotateCw size={15} />
          {retryLabel}
        </button>
      )}
    </div>
  );
}
