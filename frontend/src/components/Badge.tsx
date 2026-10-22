import type { ReactNode } from "react";

export type BadgeTone =
  | "neutral"
  | "accent"
  | "info"
  | "success"
  | "warning"
  | "danger";

interface BadgeProps {
  children: ReactNode;
  tone?: BadgeTone;
  icon?: ReactNode;
  className?: string;
  title?: string;
}

export function Badge({
  children,
  tone = "neutral",
  icon,
  className = "",
  title,
}: BadgeProps) {
  return (
    <span className={`badge badge-${tone} ${className}`.trim()} title={title}>
      {icon}
      {children}
    </span>
  );
}
