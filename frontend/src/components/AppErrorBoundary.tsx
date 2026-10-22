import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { AlertTriangle, RotateCw } from "lucide-react";

interface ErrorBoundaryState {
  hasError: boolean;
  message: string;
}

export class AppErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { hasError: false, message: "" };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return {
      hasError: true,
      message:
        error instanceof Error && error.message
          ? error.message
          : "An unexpected UI error occurred.",
    };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled UI error", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="fatal-error">
          <span className="fatal-error-icon">
            <AlertTriangle size={28} />
          </span>
          <h1>The workspace hit an unexpected error</h1>
          <p>{this.state.message}</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => window.location.reload()}
          >
            <RotateCw size={17} />
            Reload the app
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
