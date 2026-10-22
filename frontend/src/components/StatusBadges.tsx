import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Clock3,
  Loader2,
  XCircle,
} from "lucide-react";

import type {
  DocumentStatus,
  WorkflowRoute,
} from "../api/types";
import { humanizeRoute } from "../lib/format";
import { Badge, type BadgeTone } from "./Badge";

const DOCUMENT_STATUS: Record<
  DocumentStatus,
  { label: string; tone: BadgeTone; icon: React.ReactNode }
> = {
  pending: {
    label: "Pending",
    tone: "neutral",
    icon: <CircleDashed size={13} />,
  },
  processing: {
    label: "Indexing",
    tone: "info",
    icon: <Loader2 size={13} className="spin" />,
  },
  ready: {
    label: "Ready",
    tone: "success",
    icon: <CheckCircle2 size={13} />,
  },
  failed: {
    label: "Failed",
    tone: "danger",
    icon: <XCircle size={13} />,
  },
};

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  const meta = DOCUMENT_STATUS[status];
  return (
    <Badge tone={meta.tone} icon={meta.icon}>
      {meta.label}
    </Badge>
  );
}

const ROUTE_TONE: Record<WorkflowRoute, BadgeTone> = {
  chat: "neutral",
  documents: "info",
  research: "accent",
  analytics: "warning",
  report: "success",
};

export function RouteBadge({ route }: { route: WorkflowRoute }) {
  return (
    <Badge tone={ROUTE_TONE[route]} className="badge-route">
      {humanizeRoute(route)}
    </Badge>
  );
}

export function GroundedBadge({ grounded }: { grounded: boolean | null }) {
  if (grounded === null) {
    return null;
  }
  return grounded ? (
    <Badge tone="success" icon={<CheckCircle2 size={13} />}>
      Grounded
    </Badge>
  ) : (
    <Badge tone="warning" icon={<AlertCircle size={13} />}>
      No evidence found
    </Badge>
  );
}

export function SearchableBadge({ count }: { count: number }) {
  return (
    <Badge tone="neutral" icon={<Clock3 size={13} />}>
      {count} chunk{count === 1 ? "" : "s"}
    </Badge>
  );
}
