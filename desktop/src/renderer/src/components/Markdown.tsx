import { lazy, memo, Suspense } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

// mermaid is a ~3MB chunk — only pay for it when a diagram actually shows up.
const Mermaid = lazy(() => import('./Mermaid'))

const PRE_CLASS =
  'mb-2.5 mt-0 overflow-x-auto rounded-lg bg-[#141414] p-3 text-[13px] leading-relaxed text-ink-2 [&>code]:rounded-none [&>code]:bg-transparent [&>code]:p-0 [&>code]:text-ink-2'

// Fixed-dark code surface in both themes: the hljs github-dark palette can't
// flip with the theme, so the block keeps a dark background instead.
const components = {
  p: ({ children }: any) => <p className="mb-2.5 last:mb-0 leading-relaxed">{children}</p>,
  h1: ({ children }: any) => (
    <h1 className="mb-2 mt-4 text-base font-semibold first:mt-0">{children}</h1>
  ),
  h2: ({ children }: any) => (
    <h2 className="mb-2 mt-4 text-[15px] font-semibold first:mt-0">{children}</h2>
  ),
  h3: ({ children }: any) => (
    <h3 className="mb-1.5 mt-3 text-sm font-semibold first:mt-0">{children}</h3>
  ),
  ul: ({ children }: any) => (
    <ul className="mb-2.5 list-disc space-y-1 pl-5 last:mb-0 marker:text-ink-4">{children}</ul>
  ),
  ol: ({ children }: any) => (
    <ol className="mb-2.5 list-decimal space-y-1 pl-5 last:mb-0 marker:text-ink-4">{children}</ol>
  ),
  li: ({ children }: any) => <li className="leading-relaxed">{children}</li>,
  blockquote: ({ children }: any) => (
    <blockquote className="my-2.5 border-l-2 border-line-strong pl-3 text-ink-3 italic">
      {children}
    </blockquote>
  ),
  a: ({ href, children }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">
      {children}
    </a>
  ),
  table: ({ children }: any) => (
    <div className="my-2.5 overflow-x-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  ),
  thead: ({ children }: any) => <thead className="bg-app/60">{children}</thead>,
  th: ({ children }: any) => (
    <th className="border-b border-line px-3 py-1.5 text-left font-medium">{children}</th>
  ),
  td: ({ children }: any) => (
    <td className="border-b border-line/60 px-3 py-1.5 align-top">{children}</td>
  ),
  hr: () => <hr className="my-4 border-line" />,
  strong: ({ children }: any) => <strong className="font-semibold text-ink">{children}</strong>,
  del: ({ children }: any) => <del className="text-ink-4 line-through">{children}</del>,
  code: ({ className, children }: any) =>
    className ? (
      <code className={`${className} text-[13px]`}>{children}</code>
    ) : (
      <code className="rounded bg-app/70 px-1 py-0.5 text-[13px] text-ink-2">{children}</code>
    ),
  pre: ({ children }: any) => {
    const child = Array.isArray(children) ? children[0] : children
    const cls: string = child?.props?.className ?? ''
    if (cls.includes('language-mermaid')) {
      const src = String(child?.props?.children ?? '').replace(/\n$/, '')
      return (
        <Suspense fallback={<pre className={PRE_CLASS}>{src}</pre>}>
          <Mermaid code={src} />
        </Suspense>
      )
    }
    return <pre className={PRE_CLASS}>{children}</pre>
  },
}

export const Markdown = memo(function Markdown({ content }: { content: string }) {
  return (
    <div className="text-sm">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
})
