import {
  Braces,
  Database,
  ExternalLink,
  FileText,
  Globe2,
  Quote,
  Search,
} from "lucide-react";

import type {
  AnalyticsEvidence,
  SourceCitation,
  TokenUsage,
  WebSource,
} from "../api/types";
import {
  cartesianColumns,
  cellValue,
  documentLocation,
  formatNumber,
  formatScore,
  formatTokens,
} from "../lib/format";
import { Badge } from "./Badge";
import { CopyButton } from "./CopyButton";

interface EvidenceSectionsProps {
  sources?: SourceCitation[];
  webSources?: WebSource[];
  analytics?: AnalyticsEvidence | null;
  searchedChunks?: number;
  compact?: boolean;
}

function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function DocumentSourceCard({ source }: { source: SourceCitation }) {
  const location = documentLocation(source.page, source.section);
  return (
    <article className="source-card">
      <div className="source-card-top">
        <span className="citation-label">[{source.label}]</span>
        <div className="source-title-wrap">
          <div className="source-title">
            <FileText size={15} />
            <span>{source.filename}</span>
          </div>
          {location && <p className="source-location">{location}</p>}
        </div>
        <Badge tone="neutral" title="Cosine similarity">
          {formatScore(source.score)}
        </Badge>
      </div>
      <p className="source-snippet">
        <Quote size={14} aria-hidden="true" />
        <span>{source.snippet}</span>
      </p>
    </article>
  );
}

function WebSourceCard({ source }: { source: WebSource }) {
  return (
    <article className="source-card">
      <div className="source-card-top">
        <span className="citation-label citation-label-web">{source.label}</span>
        <div className="source-title-wrap">
          <a
            className="source-title source-title-link"
            href={source.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            <Globe2 size={15} />
            <span>{source.title}</span>
            <ExternalLink size={13} />
          </a>
          <p className="source-location">{hostname(source.url)}</p>
        </div>
        {source.score !== null && (
          <Badge tone="neutral" title="Provider relevance score">
            {formatScore(source.score)}
          </Badge>
        )}
      </div>
      <p className="source-snippet">
        <Quote size={14} aria-hidden="true" />
        <span>{source.snippet}</span>
      </p>
    </article>
  );
}

function AnalyticsView({ analytics }: { analytics: AnalyticsEvidence }) {
  const columns = cartesianColumns(analytics.rows);

  return (
    <div className="analytics-view">
      <div className="analytics-summary">
        <Badge tone="warning" icon={<Database size={13} />}>
          {formatNumber(analytics.row_count)} row
          {analytics.row_count === 1 ? "" : "s"}
        </Badge>
        {analytics.truncated && (
          <Badge tone="warning">Row cap reached</Badge>
        )}
        <CopyButton text={analytics.sql} label="Copy SQL" />
      </div>

      <pre className="sql-block" aria-label="Executed SQL">
        <code>{analytics.sql}</code>
      </pre>

      {analytics.rows.length === 0 ? (
        <p className="muted-note">The query returned no rows.</p>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {analytics.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((column) => (
                    <td key={column}>{cellValue(row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function EvidenceSections({
  sources = [],
  webSources = [],
  analytics = null,
  searchedChunks,
  compact = false,
}: EvidenceSectionsProps) {
  const hasEvidence =
    sources.length > 0 || webSources.length > 0 || analytics !== null;

  if (!hasEvidence) {
    return null;
  }

  return (
    <div className={`evidence-sections ${compact ? "evidence-compact" : ""}`.trim()}>
      {sources.length > 0 && (
        <section className="evidence-section">
          <div className="evidence-heading">
            <span>
              <FileText size={16} />
              Document evidence
            </span>
            <span className="evidence-count">{sources.length}</span>
          </div>
          {searchedChunks !== undefined && searchedChunks > 0 && (
            <p className="evidence-note">
              <Search size={13} />
              Compared {formatNumber(searchedChunks)} indexed passage
              {searchedChunks === 1 ? "" : "s"}.
            </p>
          )}
          <div className="source-list">
            {sources.map((source) => (
              <DocumentSourceCard key={`${source.document_id}-${source.label}`} source={source} />
            ))}
          </div>
        </section>
      )}

      {webSources.length > 0 && (
        <section className="evidence-section">
          <div className="evidence-heading">
            <span>
              <Globe2 size={16} />
              Web evidence
            </span>
            <span className="evidence-count">{webSources.length}</span>
          </div>
          <div className="source-list">
            {webSources.map((source) => (
              <WebSourceCard key={source.label} source={source} />
            ))}
          </div>
        </section>
      )}

      {analytics && (
        <section className="evidence-section">
          <div className="evidence-heading">
            <span>
              <Braces size={16} />
              Analytics evidence
            </span>
            <span className="evidence-count">SQL</span>
          </div>
          <AnalyticsView analytics={analytics} />
        </section>
      )}
    </div>
  );
}

interface ResponseMetaProps {
  provider: string;
  model: string;
  usage: TokenUsage | null;
  requestId: string;
}

export function ResponseMeta({
  provider,
  model,
  usage,
  requestId,
}: ResponseMetaProps) {
  return (
    <div className="response-meta">
      <span>
        {provider} · {model}
      </span>
      {usage && (
        <span title="Input / output / total tokens">
          {formatTokens(usage.input_tokens)} in ·{" "}
          {formatTokens(usage.output_tokens)} out ·{" "}
          {formatTokens(usage.total_tokens)} total
        </span>
      )}
      <span className="response-request">
        request <code>{requestId}</code>
      </span>
    </div>
  );
}
