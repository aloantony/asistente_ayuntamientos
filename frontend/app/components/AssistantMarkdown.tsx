import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const ASSISTANT_MARKDOWN_ELEMENTS = [
  "p",
  "strong",
  "em",
  "ul",
  "ol",
  "li",
  "blockquote",
  "code",
  "pre",
  "a",
  "h1",
  "h2",
  "h3",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "hr",
  "br",
];

type AssistantMarkdownProps = {
  content: string;
};

export function AssistantMarkdown({ content }: AssistantMarkdownProps) {
  return (
    <ReactMarkdown
      allowedElements={ASSISTANT_MARKDOWN_ELEMENTS}
      components={{
        a: ({ href, children }) => (
          <a href={href} rel="noreferrer" target="_blank">
            {children}
          </a>
        ),
        table: ({ children }) => (
          <div
            aria-label="Tabla de la respuesta"
            className="assistant-markdown-table-scroll"
            role="region"
            tabIndex={0}
          >
            <table>{children}</table>
          </div>
        ),
      }}
      remarkPlugins={[remarkGfm]}
    >
      {content}
    </ReactMarkdown>
  );
}
