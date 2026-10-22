import { Loader2 } from "lucide-react";

interface SpinnerProps {
  label?: string;
  size?: number;
  className?: string;
}

export function Spinner({ label = "Loading", size = 18, className = "" }: SpinnerProps) {
  return (
    <span className={`spinner-wrap ${className}`.trim()} role="status">
      <Loader2 className="spin" size={size} aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </span>
  );
}
