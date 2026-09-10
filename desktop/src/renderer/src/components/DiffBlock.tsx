import { useMemo, useState, type ReactNode } from 'react'
import { ChevronRight, FileDiff } from 'lucide-react'
import { cn } from '../lib/cn'

interface Props {
  /** Absolute path from the tool result JSON (already resolve()d server-side —
   *  the args path may still be relative). */
  path: string
  /** Unified diff text as embedded by write_file/patch results (≤5000 chars). */
  unified: string
  /** Extra header actions (打开文件 / 打开对比 buttons live here). */
  actions?: ReactNode
}

function lineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'text-ink-4'
  if (line.startsWith('@@')) return 'text-accent'
  if (line.startsWith('+')) return 'text-success'
  if (line.startsWith('-')) return 'text-danger'
  return 'text-ink-4'
}

/** Collapsed-by-default unified-diff card for write_file/patch tool results.
 *  Line coloring is hand-rolled off the unified-diff markers — no highlight.js
 *  dependency, and the grammar is exactly the line prefixes. */
export function DiffBlock({ path, unified, actions }: Props) {
  const [open, setOpen] = useState(false)
  const base = path.split(/[\\/]/).filter(Boolean).pop() ?? path
  const lines = useMemo(() => unified.split('\n'), [unified])
  const stats = useMemo(() => {
    let add = 0
    let del = 0
    for (const l of lines) {
      if (l.startsWith('+++') || l.startsWith('---')) continue
      else if (l.startsWith('+')) add++
      else if (l.startsWith('-')) del++
    }
    return { add, del }
  }, [lines])

  return (
    <div className="mt-1 overflow-hidden rounded-md border border-line bg-app/60">
      <div className="flex items-center gap-1.5 px-2 py-1 text-[10px] text-ink-4">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left hover:text-ink-3"
          title={path}
        >
          <ChevronRight className={cn('h-3 w-3 shrink-0 transition-transform', open && 'rotate-90')} />
          <FileDiff className="h-3 w-3 shrink-0 text-accent" />
          <span className="truncate font-mono text-[11px] text-ink-2">{base}</span>
          <span className="shrink-0 tabular-nums text-success/90">+{stats.add}</span>
          <span className="shrink-0 tabular-nums text-danger/90">−{stats.del}</span>
        </button>
        <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
      </div>
      {open && (
        <pre className="max-h-80 overflow-auto whitespace-pre border-t border-line/70 px-2 py-1.5 font-mono text-[11px] leading-relaxed">
          {lines.map((l, i) => (
            <span key={i} className={cn('block', lineClass(l))}>
              {l || ' '}
            </span>
          ))}
        </pre>
      )}
    </div>
  )
}
