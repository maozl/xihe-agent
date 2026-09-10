import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Play, History, Square } from 'lucide-react'
import { desktop, type RunEvent } from '../lib/desktop'
import { useStore } from '../store'
import { cn } from '../lib/cn'
import { Resizer, usePanelSize } from './Resizer'

const MIN_HEIGHT = 160
const DEFAULT_HEIGHT = 300
const HEIGHT_KEY = 'runPanelHeight'
// Output retained in the renderer — head is dropped beyond this so a chatty
// long run can't grow the DOM (and the string) without bound.
const OUTPUT_CAP = 200_000
const ANSI_RE = /\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g

interface Segment {
  stream: 'out' | 'err'
  text: string
}

/** One workdir's run slot: the command, live output, and exit state. */
interface Slot {
  command: string
  runId: number | null
  exit: number | null
  segments: Segment[]
}

/** Bottom drawer: one-shot local runs with a command input whose
 *  per-workspace history persists across sessions — ↑ recalls, the dropdown
 *  re-fills, so parameterized commands never need retyping. Each workspace
 *  gets its own slot, so two workspaces can run their builds side by side. */
export function RunPanel({ className }: { className?: string }) {
  const [height, setHeight] = usePanelSize(HEIGHT_KEY, DEFAULT_HEIGHT, MIN_HEIGHT)
  const [cmd, setCmd] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [histOpen, setHistOpen] = useState(false)
  // workdir → that workspace's slot (kept across switches; the view follows
  // the active workspace).
  const [slots, setSlots] = useState<Record<string, Slot>>({})

  const agents = useStore((s) => s.agents)
  const selectedAgentId = useStore((s) => s.selectedAgentId)
  const convWorkspace = useStore((s) => s.convWorkspace)
  const workspaces = useStore((s) => s.workspaces)
  const runCmdDraft = useStore((s) => s.runCmdDraft)
  const clearRunCmdDraft = useStore((s) => s.clearRunCmdDraft)

  const agent = agents.find((a) => a.id === selectedAgentId) ?? agents[0]
  const convId = agent?.activeConvId
  const wsId = convId ? convWorkspace[convId] : undefined
  const activeWs = wsId ? workspaces.find((w) => w.id === wsId) : undefined
  const slot = activeWs ? slots[activeWs.workdir] : undefined
  const segments = slot?.segments ?? []
  const exit = slot?.exit ?? null
  const running = slot?.runId != null

  const outRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  // ↑/↓ navigation cursor into the history list (null = not navigating).
  const histNavRef = useRef<number | null>(null)

  // History follows the active workspace (per-workdir buckets in main).
  useEffect(() => {
    let alive = true
    void desktop.runHistory().then((all) => {
      if (!alive) return
      setHistory(activeWs ? all[activeWs.workdir] ?? [] : [])
    })
    return () => {
      alive = false
    }
  }, [activeWs?.workdir, running])

  // One-shot prefill from a Play entry point (editor tab / tree row).
  useEffect(() => {
    if (runCmdDraft != null) {
      setCmd(runCmdDraft)
      clearRunCmdDraft()
    }
  }, [runCmdDraft, clearRunCmdDraft])

  // Run events, routed to the owning workdir's slot by the run id (start
  // carries the cwd; out/exit match whichever slot holds the id). Stale ids
  // (a superseded run's exit arriving late) match no slot and are dropped.
  useEffect(() => {
    const off = desktop.onRunEvent((ev: RunEvent) => {
      setSlots((prev) => {
        if (ev.t === 'start') {
          stickRef.current = true
          return { ...prev, [ev.cwd]: { command: ev.command, runId: ev.id, exit: null, segments: [] } }
        }
        const next = { ...prev }
        for (const [wd, s] of Object.entries(prev)) {
          if (s.runId !== ev.id) continue
          if (ev.t === 'out') {
            const segs = s.segments
            const last = segs[segs.length - 1]
            let updated =
              last && last.stream === ev.stream
                ? [...segs.slice(0, -1), { stream: ev.stream, text: last.text + ev.chunk }]
                : [...segs, { stream: ev.stream, text: ev.chunk }]
            let total = 0
            for (let i = updated.length - 1; i >= 0; i--) {
              total += updated[i].text.length
              if (total > OUTPUT_CAP) updated = updated.slice(i + 1)
            }
            next[wd] = { ...s, segments: updated }
          } else {
            next[wd] = { ...s, exit: ev.code, runId: null }
          }
        }
        return next
      })
    })
    return off
  }, [])

  // Follow output while near the bottom; reading history keeps the view put.
  useEffect(() => {
    if (!stickRef.current) return
    outRef.current?.scrollTo({ top: outRef.current.scrollHeight })
  }, [segments])

  const start = (): void => {
    const command = cmd.trim()
    if (!command || !activeWs) return
    histNavRef.current = null
    void desktop.runStart(command, activeWs.workdir)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') {
      e.preventDefault()
      start()
    } else if (e.key === 'ArrowUp' && history.length > 0) {
      e.preventDefault()
      const cur = histNavRef.current
      const idx = cur == null ? 0 : Math.min(cur + 1, history.length - 1)
      histNavRef.current = idx
      setCmd(history[idx])
    } else if (e.key === 'ArrowDown' && histNavRef.current != null) {
      e.preventDefault()
      const idx = histNavRef.current - 1
      histNavRef.current = idx >= 0 ? idx : null
      setCmd(idx >= 0 ? history[idx] : '')
    }
  }

  // Height handle (top edge) — shared Resizer.
  return (
    <section className={cn('relative flex flex-col bg-app', className)} style={{ height }}>
      <Resizer
        axis="row"
        title="拖动调整面板高度"
        onMove={(_x, y) => {
          const max = Math.max(window.innerHeight - 240, MIN_HEIGHT)
          setHeight(Math.min(Math.max(window.innerHeight - y, MIN_HEIGHT), max))
        }}
        className="absolute left-0 top-0 h-1.5 w-full -translate-y-1/2"
      />

      <header className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-1.5">
        <span className="shrink-0 text-xs text-ink-4">运行</span>
        <span
          className="max-w-[14rem] shrink-0 truncate rounded bg-elevated px-1.5 py-0.5 text-[10px] text-ink-3"
          title={activeWs?.workdir}
        >
          {activeWs ? activeWs.name : '未绑定工作空间'}
        </span>
        <div className="relative flex min-w-0 flex-1 items-center gap-1">
          <input
            value={cmd}
            onChange={(e) => setCmd(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            placeholder={
              activeWs ? '命令（↑ 召回历史）…' : '先在「工作空间」绑定一个工作空间'
            }
            className="min-w-0 flex-1 rounded-md border border-line-strong bg-panel px-2 py-1 font-mono text-xs text-ink outline-none focus:border-accent"
          />
          <button
            onClick={() => setHistOpen((v) => !v)}
            disabled={history.length === 0}
            title="历史命令"
            className={cn(
              'shrink-0 rounded p-1.5 transition',
              histOpen
                ? 'bg-contrast text-sky-300'
                : 'text-ink-3 hover:bg-elevated hover:text-ink disabled:opacity-30'
            )}
          >
            <History className="h-3.5 w-3.5" />
          </button>
          {histOpen && (
            <>
              <button className="fixed inset-0 z-10 cursor-default" aria-hidden onClick={() => setHistOpen(false)} />
              <div className="absolute bottom-full right-0 z-20 mb-1 max-h-64 w-[28rem] max-w-full overflow-auto rounded-lg border border-line-strong bg-panel py-1 shadow-xl">
                {history.map((h, i) => (
                  <button
                    key={`${i}-${h}`}
                    onClick={() => {
                      setCmd(h)
                      setHistOpen(false)
                      histNavRef.current = null
                    }}
                    className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left font-mono text-xs text-ink-2 transition hover:bg-elevated"
                  >
                    <ChevronDown className="h-3 w-3 shrink-0 rotate-[-90deg] text-ink-4" />
                    <span className="truncate">{h}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        {running ? (
          <button
            onClick={() => activeWs && void desktop.runStop(activeWs.workdir)}
            title="停止"
            className="flex shrink-0 items-center gap-1 rounded-lg bg-rose-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-rose-500"
          >
            <Square className="h-3 w-3" /> 停止
          </button>
        ) : (
          <button
            onClick={start}
            disabled={!cmd.trim() || !activeWs}
            title="运行（Enter）"
            className="flex shrink-0 items-center gap-1 rounded-lg bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-30"
          >
            <Play className="h-3 w-3" /> 运行
          </button>
        )}
      </header>

      <div
        ref={outRef}
        onScroll={() => {
          const el = outRef.current
          if (!el) return
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
        }}
        className="min-h-0 flex-1 overflow-auto bg-[#0a0a0a] px-3 py-2 font-mono text-xs leading-relaxed"
      >
        {segments.length === 0 && exit === null && (
          <div className="text-ink-4">
            输出显示在这里。适合一次性运行（脚本、测试、构建）；交互式或长驻程序请用终端面板的「本地」标签。
          </div>
        )}
        {segments.map((s, i) => (
          <pre
            key={i}
            className={cn(
              'whitespace-pre-wrap break-words',
              s.stream === 'err' ? 'text-rose-300' : 'text-[#d4d4d4]'
            )}
          >
            {s.text.replace(ANSI_RE, '')}
          </pre>
        ))}
        {exit !== null && (
          <div className={cn('mt-1', exit === 0 ? 'text-emerald-400' : 'text-rose-400')}>
            —— 进程已退出（{exit === 0 ? '成功' : `代码 ${exit ?? '无'}`}）——
          </div>
        )}
        {running && <div className="text-ink-4">运行中…</div>}
      </div>
    </section>
  )
}
