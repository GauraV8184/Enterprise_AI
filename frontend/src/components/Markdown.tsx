import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownProps {
  children: string;
  className?: string;
}

/**
 * Markdown is rendered without raw HTML. ReactMarkdown escapes HTML by default;
 * adding rehype-raw here would turn model and document output into executable
 * page content, so it is deliberately absent.
 */
export function Markdown({ children, className = "" }: MarkdownProps) {
  return (
    <div className={`markdown ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: linkChildren }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
            >
              {linkChildren}
            </a>
          ),
          table: ({ children: tableChildren }) => (
            <div className="markdown-table-wrap">
              <table>{tableChildren}</table>
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
