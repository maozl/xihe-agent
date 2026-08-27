import { memo, useEffect, useMemo, useState, type MouseEvent } from 'react'
import { Brain, Check, ChevronRight, Copy, Loader2, Navigation, Wrench, X } from 'lucide-react'
import type { TraceEvent } from '../store'
import { cn } from '../lib/cn'
import { getFullToolResult } from '../lib/serveClient'

type ToolEv = Extract<TraceEvent, { kind: 'tool' }>
type ThoughtEv = Extract<TraceEvent, { kind: 'thought' }>
type SteerEv = Extract<TraceEvent, { kind: 'steer' }>

/** Collapsible trace of one assistant turn's reasoning + tool calls, rendered
 * ABOVE the answer bubble. Events appear in arrival order — 思考 and 工具调用
 * interleave exactly as they did during the turn, since reasoning and tool
 * calls alternate mid-turn; splitting them into separate cards would lose that
 * ordering. Open state is fully user-controlled; initial state follows
 * `pending` (a running turn mounts open so live progress shows; a finished or
 * historical turn mounts collapsed).
 *
 * Historical turns arrive with only a folded tool count, a serve row anchor,
 * and a `hasReasoning` flag — reasoning + tool calls are lazy-loaded via
 * `onLoadTrace` on expand. */
export function TurnTrace({
  trace,
  pending,
  toolsCount,
  hasReasoning,
  anchor,
  onLoadTrace,
}: {
  trace?: TraceEvent[]
  pending: boolean
  /** Historical turn: tool count shown in the header before the trace is
   *  lazy-loaded. Live turns carry the real list in `trace` instead. */
  toolsCount?: number
  /** Historical turn: serve reported persisted reasoning — shows a 思考 badge
   *  in the collapsed header before the trace is lazy-loaded. */
  hasReasoning?: boolean
  /** Serve row id used to lazy-load the trace (historical turns only). */
  anchor?: number
  /** Expand hook that fires the lazy load. */
  onLoadTrace?: (anchor: number) => void
}) {
  const [open, setOpen] = useState(pending)
  const events = trace ?? []
  const tools = events.filter((t): t is ToolEv => t.kind === 'tool')
  const running = tools.filter((t) => t.status === 'running').length
  const interruptedTools = tools.filter((t) => t.status === 'interrupted').length
  const hasThought = events.some((t): t is ThoughtEv => t.kind === 'thought')
  const steerCount = events.filter((t): t is SteerEv => t.kind === 'steer').length
  // Header count: live trace has the real list; a historical turn shows its
  // folded count until the trace is lazy-loaded on expand.
  const toolCount = toolsCount ?? tools.length
  // Lazy-load: expanding a historical turn (trace not yet fetched) pulls it.
  useEffect(() => {
    if (open && !trace && anchor != null && onLoadTrace) onLoadTrace(anchor)
    // onLoadTrace is a per-render inline closure; intentionally omitted from
    // deps so a re-render mid-fetch doesn't fire duplicate loads. Live turns
    // have anchor=undefined, so this never fires for them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, trace, anchor])

  if (!toolCount && !hasThought && !steerCount && !hasReasoning) return null

  // Click anywhere on the panel (not just the header) toggles — a long 思考
  // run scrolls the header out of reach otherwise. Clicks on interactive
  // children (tool rows, result blocks, links) and clicks that end a text
  // selection pass through untouched.
  const onPanelClick = (e: MouseEvent) => {
    const t = e.target as HTMLElement
    if (t.closest('button, a, input, textarea, pre')) return
    if (window.getSelection()?.toString()) return
    setOpen((v) => !v)
  }

  return (
    <div
      onClick={onPanelClick}
      className="w-full cursor-pointer rounded-xl border border-line bg-panel/60 text-xs text-ink-3"
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:text-ink"
      >
        <ChevronRight className={cn('h-3 w-3 shrink-0 transition-transform', open && 'rotate-90')} />
        {toolCount > 0 && (
          <span className="inline-flex items-center gap-1">
            <Wrench className="h-3 w-3" />
            {toolCount} 个工具
            {running > 0 ? ` · ${running} 运行中` : ''}
            {interruptedTools > 0 ? ` · ${interruptedTools} 已中断` : ''}
          </span>
        )}
        {(hasThought || hasReasoning) && (
          <span className="inline-flex items-center gap-1">
            <Brain className="h-3 w-3" />
            思考
          </span>
        )}
        {steerCount > 0 && (
          <span className="inline-flex items-center gap-1">
            <Navigation className="h-3 w-3" />
            {steerCount} 改向
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-line/70 px-2.5 py-2">
          {trace ? (
            events.map((t, i) =>
              t.kind === 'tool' ? (
                <ToolItem key={i} ev={t} />
              ) : t.kind === 'thought' ? (
                <ThoughtItem key={i} ev={t} />
              ) : (
                <SteerItem key={i} ev={t} />
              )
            )
          ) : (
            <div className="px-1 py-0.5 text-ink-4">加载工具调用…</div>
          )}
        </div>
      )}
    </div>
  )
}

const ARGS_PREVIEW = 60

/** Source tag on trace rows: absent = the main agent; otherwise the child's
 *  label — a specialist slug, a per-child "subagent-N" (delegate_task), or
 *  "claude" for the external claude CLI's own tool/thinking activity (an
 *  inner source wins over the outer label). */
function ByBadge({ by }: { by?: string }) {
  if (!by) return null
  return (
    <span className="shrink-0 rounded bg-elevated px-1 py-0.5 font-mono text-[10px] text-ink-3">
      {by}
    </span>
  )
}

const ToolItem = memo(function ToolItem({ ev }: { ev: ToolEv }) {
  const [expanded, setExpanded] = useState(false)
  const tooLong = ev.args.length > ARGS_PREVIEW
  const ts = fmtTs(ev.ts)
  return (
    <div className="flex items-start gap-1.5">
      <span className="mt-0.5 inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center">
        {ev.status === 'running' ? (
          <Loader2 className="h-3 w-3 animate-spin text-ink-4" />
        ) : ev.status === 'interrupted' ? (
          <X className="h-3 w-3 text-warning" />
        ) : (
          <Check className="h-3 w-3 text-success/80" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <ByBadge by={ev.by} />
          <span className="rounded bg-elevated px-1 py-0.5 font-mono text-[11px] text-ink-2">
            {ev.name}
          </span>
          {ev.status === 'interrupted' ? (
            <span className="text-[10px] text-warning">已中断</span>
          ) : typeof ev.elapsed === 'number' ? (
            <span className="text-[10px] text-ink-4">{ev.elapsed}s</span>
          ) : null}
          {ts && <span className="ml-auto text-[10px] tabular-nums text-ink-4">{ts}</span>}
        </div>
        {ev.args && (
          <button
            type="button"
            onClick={() => tooLong && setExpanded((v) => !v)}
            className={cn(
              'mt-0.5 block w-full text-left font-mono text-ink-4 hover:text-ink-3',
              expanded ? 'whitespace-pre-wrap break-all' : 'truncate'
            )}
            title={ev.args}
          >
            {expanded ? ev.args : ev.args.slice(0, ARGS_PREVIEW)}
            {tooLong && !expanded && '…'}
          </button>
        )}
        {ev.status === 'done' && ev.result != null && (
          <ResultBlock result={ev.result} truncated={ev.truncated} />
        )}
      </div>
    </div>
  )
})

const THOUGHT_PREVIEW = 160

const ThoughtItem = memo(function ThoughtItem({ ev }: { ev: ThoughtEv }) {
  const [expanded, setExpanded] = useState(false)
  const tooLong = ev.text.length > THOUGHT_PREVIEW
  const ts = fmtTs(ev.ts)
  return (
    <div className="flex items-start gap-1.5">
      <Brain className="mt-0.5 h-3 w-3 shrink-0 text-ink-4" />
      <div className="min-w-0 flex-1">
        <div className="flex items-start gap-1.5">
          <ByBadge by={ev.by} />
          <p
            className={cn(
              'min-w-0 flex-1 whitespace-pre-wrap italic text-ink-4',
              !expanded && tooLong && 'line-clamp-2'
            )}
          >
            {/* Collapsed: clamp the DOM node too, not just paint — a long
                claude/codex thinking run would otherwise put tens of KB into a
                text node re-diffed on every store flush. */}
            {!expanded && tooLong ? `${ev.text.slice(0, 400)}…` : ev.text}
          </p>
          {ts && (
            <span className="shrink-0 text-[10px] tabular-nums text-ink-4">{ts}</span>
          )}
        </div>
        {tooLong && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-[10px] text-ink-4 hover:text-ink-3"
          >
            {expanded ? '收起' : '展开'}
          </button>
        )}
      </div>
    </div>
  )
})

function SteerItem({ ev }: { ev: SteerEv }) {
  // A user redirect injected mid-turn (not a tool/thought). Set apart with an
  // amber accent so it reads as the user's input, not the agent's activity.
  return (
    <div className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5">
      <Navigation className="mt-0.5 h-3 w-3 shrink-0 text-warning/80" />
      <div className="min-w-0 flex-1">
        <span className="text-[10px] uppercase tracking-wide text-warning/70">steer · 改向</span>
        <p className="whitespace-pre-wrap break-words text-warning/70">{ev.text}</p>
      </div>
    </div>
  )
}

/** HH:MM:SS for a live-trace event ts (client epoch). Historical traces carry no
 *  ts (serve's trace endpoint has no per-call timestamps), so this returns null
 *  there and the caller omits the stamp. */
function fmtTs(ts?: number): string | null {
  if (!ts) return null
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// Lines shown before a result card collapses. Long outputs (file reads, search
// dumps) stay scannable; expand reveals the full server-truncated payload.
const RESULT_COLLAPSE_LINES = 8

// Marker line the serve-side spill writes into the preview ("Full output
// saved to: <path>") — the side-store path /toolresult serves.
const SPILL_PATH_RE = /Full output saved to: (.+)/

function ResultBlock({ result, truncated }: { result: string; truncated?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  // Full spilled content fetched from /toolresult (replaces the preview once
  // loaded); `loading`/`failed` gate the fetch button's states.
  const [full, setFull] = useState<string | null>(null)
  const [loadingFull, setLoadingFull] = useState(false)
  const [failed, setFailed] = useState(false)
  const spillPath = truncated ? result.match(SPILL_PATH_RE)?.[1]?.trim() : undefined
  const text = full ?? result
  // split cost scales with the (up-to-15K) payload — memoize so renders that
  // don't change the text (copy flash, expand toggles elsewhere) skip it.
  const lines = useMemo(() => text.split('\n'), [text])
  const lineCount = lines.length
  const collapsible = lineCount > RESULT_COLLAPSE_LINES
  const shown =
    expanded || !collapsible ? text : lines.slice(0, RESULT_COLLAPSE_LINES).join('\n')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      // clipboard may be unavailable (permissions / non-secure context) — no-op
    }
  }
  const loadFull = async () => {
    if (!spillPath || loadingFull || full != null) return
    setLoadingFull(true)
    try {
      const content = await getFullToolResult(spillPath)
      if (content == null) {
        setFailed(true)
      } else {
        setFull(content)
        setExpanded(true)
      }
    } finally {
      setLoadingFull(false)
    }
  }
  return (
    <div className="mt-1 overflow-hidden rounded-md border border-line bg-app/60">
      <div className="flex items-center gap-1.5 border-b border-line/70 px-2 py-1 text-[10px] text-ink-4">
        <span>输出</span>
        <span className="rounded bg-elevated px-1 py-0.5 text-ink-3 tabular-nums">
          {lineCount} 行
        </span>
        {spillPath ? (
          full == null ? (
            <button
              type="button"
              onClick={loadFull}
              disabled={loadingFull}
              className="inline-flex items-center gap-1 text-warning/80 hover:text-warning disabled:opacity-50"
            >
              {loadingFull ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              {loadingFull ? '加载中…' : failed ? '加载失败，重试' : '加载完整结果'}
            </button>
          ) : (
            <span className="text-success/80">已加载完整结果</span>
          )
        ) : (
          truncated && <span className="text-warning/80">已截断（完整结果见服务端会话）</span>
        )}
        <button
          type="button"
          onClick={copy}
          className="ml-auto inline-flex items-center gap-1 hover:text-ink-2"
        >
          {copied ? (
            <Check className="h-3 w-3 text-success" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
          {copied ? '已复制' : '复制'}
        </button>
        {collapsible && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="hover:text-ink-2"
          >
            {expanded ? '收起' : '展开'}
          </button>
        )}
      </div>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words px-2 py-1.5 font-mono text-[11px] leading-relaxed text-ink-3">
        {shown}
        {!expanded && collapsible && '\n…'}
      </pre>
    </div>
  )
}
