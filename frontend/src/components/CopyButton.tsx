import { useState } from "react";
import { Check, Copy } from "lucide-react";

interface CopyButtonProps {
  text: string;
  label?: string;
  copiedLabel?: string;
  className?: string;
}

export function CopyButton({
  text,
  label = "Copy",
  copiedLabel = "Copied",
  className = "btn btn-ghost btn-sm",
}: CopyButtonProps) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("copied");
      window.setTimeout(() => setStatus("idle"), 1800);
    } catch {
      setStatus("failed");
      window.setTimeout(() => setStatus("idle"), 2200);
    }
  }

  return (
    <button type="button" className={className} onClick={() => void copy()}>
      {status === "copied" ? <Check size={15} /> : <Copy size={15} />}
      {status === "copied" ? copiedLabel : status === "failed" ? "Copy failed" : label}
    </button>
  );
}
