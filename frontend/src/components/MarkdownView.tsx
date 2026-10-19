import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** The bid renderer embeds internal page-break and cover markers as comments. */
function stripInternalComments(text: string): string {
  return text.replace(/<!--[\s\S]*?-->/g, '')
}

export function MarkdownView({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} rel="noreferrer" target="_blank" />
          ),
          table: ({ node: _node, ...props }) => (
            <div className="table-scroll">
              <table {...props} />
            </div>
          ),
        }}
        remarkPlugins={[remarkGfm]}
      >
        {stripInternalComments(text)}
      </ReactMarkdown>
    </div>
  )
}
