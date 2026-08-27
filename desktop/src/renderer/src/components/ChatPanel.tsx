import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Check,
  Copy,
  FileText,
  Navigation,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings,
  ShieldAlert,
  Square,
} from 'lucide-react'
import { useStore, type Agent, type Message, type PendingApproval } from '../store'
import { cn } from '../lib/cn'
import { desktop } from '../lib/desktop'
import { TurnTrace } from './TurnTrace'
import { Markdown } from './Markdown'

/** 1234 → "1.2k"; under 1000 stays plain digits. */
function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

/** Clipboard with a legacy execCommand fallback — navigator.clipboard is
 *  unavailable when the renderer isn't a secure context. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      ta.remove()
      return ok
    } catch {
      return false
    }
  }
}

/** Icon button revealed on row hover (user-bubble actions). */
function HoverIconBtn({ title, disabled, onClick, children }: {
  title: string
  disabled?: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="mb-1 flex h-6 w-6 items-center justify-center rounded-md text-ink-4 opacity-0 hover:bg-elevated hover:text-ink-2 disabled:opacity-0 group-hover:opacity-100 disabled:group-hover:opacity-30"
    >
      {children}
    </button>
  )
}

function UsageBadge({ usage }: { usage: Message['usage'] }) {
  if (!usage) return null
  const title = `输入 ${usage.prompt} / 输出 ${usage.completion} tokens（${usage.calls ?? '?'} 次调用）`
  return (
    <div
      title={title}
      className="w-fit select-none rounded-md bg-elevated/60 px-1.5 py-0.5 text-[10px] text-ink-4"
    >
      ↑ {fmtTokens(usage.prompt)} ↓ {fmtTokens(usage.completion)}
    </div>
  )
}

/** First-run card — replaces the chat empty state while the model connection
 *  is unconfigured (api_key unset). Strict `=== false` so the pre-hydration
 *  flash (xiheConfig={}) keeps the ordinary empty state. */
function WelcomeCard({ onGoSettings }: { onGoSettings: () => void }) {
  return (
    <div className="max-w-md rounded-2xl border border-line bg-elevated px-6 py-5 text-center">
      <div className="text-base font-semibold text-ink">欢迎使用xihe</div>
      <div className="mt-2 text-sm leading-relaxed text-ink-3">
        还差一步：配置模型连接后就能开始对话。在设置页填写 API Key
        并保存即可（会自动重启xihe生效）。
      </div>
      <button
        onClick={onGoSettings}
        className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90"
      >
        <Settings className="h-4 w-4" />
        去配置模型连接
      </button>
    </div>
  )
}

/** Terminal install-state card — replaces the chat empty state when the serve
 *  child couldn't even spawn (no `xihe` on PATH). serve parks in not_found
 *  (retrying can't help), so recovery is user action + the retry button. */
function XiheMissingCard({ message }: { message?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    if (!(await copyText('pip install -e .'))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="max-w-md rounded-2xl border border-danger/50 bg-elevated px-6 py-5 text-center">
      <div className="text-base font-semibold text-ink">未找到 xihe 命令</div>
      <div className="mt-2 text-sm leading-relaxed text-ink-3">
        桌面版依赖 xihe CLI。在仓库根目录安装后再回来重试：
      </div>
      <div className="mt-3 flex items-center justify-center gap-1.5">
        <code className="rounded-md bg-panel px-2.5 py-1 text-xs text-ink-2">pip install -e .</code>
        <button
          onClick={() => void copy()}
          title="复制安装命令"
          className="rounded-md p-1.5 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      </div>
      <div className="mt-2 text-xs text-ink-4">
        安装在别处？设 XIHE_BIN 环境变量指向 xihe 可执行文件后重启桌面版
      </div>
      <div className="mt-4 flex items-center justify-center gap-2">
        <button
          onClick={() => void desktop.serveRestart()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90"
        >
          <RefreshCw className="h-4 w-4" />
          已安装，重试
        </button>
        <button
          onClick={() => void desktop.openServeLog()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-4 py-2 text-sm text-ink-2 transition hover:bg-elevated"
        >
          <FileText className="h-4 w-4" />
          查看日志
        </button>
      </div>
      {message && <div className="mt-3 break-all text-[11px] text-danger/70">{message}</div>}
    </div>
  )
}

/** Approval card — surfaces a dangerous operation blocked mid-turn for the
 *  user's verdict. Buttons only while the turn (and the request) is live;
 *  settled/expired cards stay as a record with a result badge. */
function ApprovalCard({ ap, running, onApprove }: {
  ap: PendingApproval
  running: boolean
  onApprove: (id: string, approved: boolean, always?: boolean) => void
}) {
  const settled =
    ap.status === 'approved' ? '已批准'
    : ap.status === 'denied' ? '已拒绝'
    : ap.status === 'expired' ? '已失效'
    : null
  return (
    <div className="w-fit rounded-xl border border-warning/40 bg-warning/10 px-3 py-2.5 text-sm">
      <div className="flex items-center gap-1.5 font-medium text-warning">
        <ShieldAlert className="h-4 w-4" />
        危险操作待确认
        <span className="text-[10px] font-normal text-ink-4">{ap.name}</span>
      </div>
      <div className="mt-1 max-w-md whitespace-pre-wrap break-all text-ink-2">{ap.summary}</div>
      {ap.status === 'pending' ? (
        running ? (
          <div className="mt-2 flex gap-2">
            <button
              onClick={() => onApprove(ap.id, true)}
              className="rounded-lg bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-500"
            >
              批准
            </button>
            <button
              onClick={() => onApprove(ap.id, true, true)}
              title="批准这一次，且本会话内相同操作不再询问"
              className="rounded-lg border border-success/50 px-3 py-1 text-xs font-medium text-success hover:bg-success/10"
            >
              批准，不再询问
            </button>
            <button
              onClick={() => onApprove(ap.id, false)}
              className="rounded-lg bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-500"
            >
              拒绝
            </button>
          </div>
        ) : (
          // Request outlived the running flag locally (socket dropped before
          // complete) — same treatment as expired, without rewriting status.
          <div className="mt-2 text-xs text-ink-4">等待答复（回合已结束，无法批复）</div>
        )
      ) : (
        <div className="mt-2">
          <span
            className={
              'rounded-md px-1.5 py-0.5 text-[10px] ' +
              (ap.status === 'approved'
                ? 'bg-success/10 text-success'
                : 'bg-danger/10 text-danger')
            }
          >
            {settled}
          </span>
        </div>
      )}
    </div>
  )
}

/** Stable-callback bundle shared by the memoized rows — the object identity is
 *  preserved across renders (useMemo in ChatPanel), so unchanged rows skip
 *  re-render entirely during a streaming turn. */
interface UserRowCbs {
  onStartEdit: (m: Message) => void
  onEditText: (t: string) => void
  onConfirmEdit: (index: number, text: string) => void
  onCancelEdit: () => void
  onCopy: (m: Message) => void
  onResend: (index: number) => void
}

interface AssistantRowCbs {
  onApprove: (id: string, approved: boolean, always?: boolean) => void
  onLoadTrace: (anchor: number) => void
  onToggleRaw: (id: string) => void
  onCopy: (m: Message) => void
  onRegenerate: (index: number) => void
  onGoSettings: () => void
}

const UserRow = memo(function UserRow({ m, i, editing, editText, copied, disabled, cbs }: {
  m: Message
  i: number
  editing: boolean
  editText: string
  copied: boolean
  disabled: boolean
  cbs: UserRowCbs
}) {
  const editTaRef = useRef<HTMLTextAreaElement>(null)
  // Auto-grow the edit textarea with its content (capped by max-h-64).
  useEffect(() => {
    if (!editing) return
    const ta = editTaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }, [editText, editing])
  return (
    <div className="flex justify-end">
      {editing ? (
        <div className="flex w-[75%] flex-col items-end gap-1.5">
          <textarea
            ref={editTaRef}
            autoFocus
            value={editText}
            onChange={(e) => cbs.onEditText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                cbs.onConfirmEdit(i, editText)
              } else if (e.key === 'Escape') {
                e.preventDefault()
                cbs.onCancelEdit()
              }
            }}
            className="max-h-64 w-full resize-none overflow-y-auto rounded-2xl rounded-br-sm border border-line bg-contrast px-3.5 py-2 text-sm text-white outline-none focus:border-brand"
          />
          <div className="flex gap-2">
            <button
              onClick={cbs.onCancelEdit}
              className="rounded-lg border border-line px-3 py-1 text-xs text-ink-3 hover:bg-elevated"
            >
              取消
            </button>
            <button
              onClick={() => cbs.onConfirmEdit(i, editText)}
              disabled={!editText.trim()}
              className="rounded-lg bg-brand px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-30"
            >
              发送
            </button>
          </div>
        </div>
      ) : (
        <div className="group flex max-w-[75%] items-end gap-1.5">
          <HoverIconBtn title="复制" onClick={() => cbs.onCopy(m)}>
            {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
          </HoverIconBtn>
          <HoverIconBtn
            title="编辑：修改这条消息后重新发送（之后的回复会被撤回）"
            disabled={disabled}
            onClick={() => cbs.onStartEdit(m)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </HoverIconBtn>
          <HoverIconBtn
            title="重新发送：撤回此消息及之后的回复，重新发送"
            disabled={disabled}
            onClick={() => cbs.onResend(i)}
          >
            <RotateCcw className="h-3.5 w-3.5" />
          </HoverIconBtn>
          <div className="whitespace-pre-wrap rounded-2xl rounded-br-sm bg-contrast px-3.5 py-2 text-sm text-white">
            {m.content}
          </div>
        </div>
      )}
    </div>
  )
})

const AssistantRow = memo(function AssistantRow({ m, i, running, pending, isRaw, copied, cbs }: {
  m: Message
  i: number
  running: boolean
  pending: boolean
  isRaw: boolean
  copied: boolean
  cbs: AssistantRowCbs
}) {
  const liveTools = m.trace?.filter((t) => t.kind === 'tool').length ?? 0
  const total = liveTools || m.toolsCount || 0
  const hasThought = m.trace?.some((t) => t.kind === 'thought')
  const hasSteer = m.trace?.some((t) => t.kind === 'steer')
  const showTrace = !!total || !!hasThought || !!hasSteer || !!m.hasReasoning
  return (
    <div className="flex justify-start">
      <div className="flex max-w-[80%] flex-col gap-1">
        {showTrace && (
          <TurnTrace
            trace={m.trace}
            pending={!!m.pending}
            toolsCount={total}
            hasReasoning={m.hasReasoning}
            anchor={m.traceAnchor}
            onLoadTrace={cbs.onLoadTrace}
          />
        )}
        {m.pendingApproval && (
          <ApprovalCard
            ap={m.pendingApproval}
            running={running}
            onApprove={cbs.onApprove}
          />
        )}
        <div
          className={cn(
            'rounded-2xl rounded-bl-sm px-3.5 py-2 text-sm',
            m.error
              ? 'border border-danger/40 bg-danger/10 text-danger'
              : 'bg-elevated text-ink'
          )}
        >
          {/* Pending turns render plain text: re-parsing the whole growing
              markdown (react-markdown + re-highlighting every code block) per
              delta was the long-turn freeze. Full markdown — including
              mermaid, whose render costs 100ms+ — mounts once the turn
              completes and this row's identity goes stable. */}
          {m.pending || isRaw ? (
            <div className="whitespace-pre-wrap break-words">{m.content}</div>
          ) : (
            <Markdown content={m.content} />
          )}
          {m.pending && !m.stopping && (() => {
            // The pending cue must stay visible for the WHOLE turn —
            // text already streamed + a long tool running otherwise
            // looks like a finished reply.
            const runningTool = m.trace?.some(
              (t) => t.kind === 'tool' && t.status === 'running'
            )
            if (runningTool)
              return <span className="text-ink-4">正在执行工具…</span>
            if (m.content === '')
              return <span className="text-ink-4">正在思考…</span>
            return (
              <span className="ml-0.5 inline-flex items-center gap-1 align-middle text-ink-4">
                <span className="inline-block h-3 w-1.5 animate-pulse bg-ink-3" />
                处理中…
              </span>
            )
          })()}
          {m.pending && m.stopping && (
            <span className="ml-1 text-warning">正在停止…</span>
          )}
        </div>
        {!m.pending && m.error && /api_key|配置/.test(m.content) && (
          <button
            onClick={cbs.onGoSettings}
            className="w-fit text-[10px] text-danger hover:text-danger/80"
          >
            打开设置
          </button>
        )}
        {!m.pending && m.content && (
          <div className="flex items-center gap-3">
            <button
              onClick={() => cbs.onToggleRaw(m.id)}
              className="select-none text-[10px] text-ink-4 hover:text-ink-2"
            >
              {isRaw ? '渲染显示' : '查看原文'}
            </button>
            <button
              onClick={() => cbs.onCopy(m)}
              className="flex select-none items-center gap-1 text-[10px] text-ink-4 hover:text-ink-2"
            >
              {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
              {copied ? '已复制' : '复制'}
            </button>
            <button
              onClick={() => cbs.onRegenerate(i)}
              disabled={pending}
              className="flex select-none items-center gap-1 text-[10px] text-ink-4 hover:text-ink-2 disabled:opacity-30"
            >
              <RefreshCw className="h-3 w-3" />
              重新生成
            </button>
          </div>
        )}
        {m.interrupted && (
          <div className="inline-flex w-fit items-center gap-1 rounded-md bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning/80">
            <Square className="h-2.5 w-2.5" />
            已停止
          </div>
        )}
        {!m.pending && !m.error && <UsageBadge usage={m.usage} />}
      </div>
    </div>
  )
})

export function ChatPanel({ agent }: { agent: Agent }) {
  const messages = useStore((s) =>
    agent.activeConvId ? s.sessions[agent.activeConvId] ?? [] : []
  )
  const sendMessage = useStore((s) => s.sendMessage)
  const newConversation = useStore((s) => s.newConversation)
  const interrupt = useStore((s) => s.interrupt)
  const steer = useStore((s) => s.steer)
  const resendMessage = useStore((s) => s.resendMessage)
  const regenerateMessage = useStore((s) => s.regenerateMessage)
  const editAndResendMessage = useStore((s) => s.editAndResendMessage)
  const approve = useStore((s) => s.approve)
  const loadTrace = useStore((s) => s.loadTrace)
  const serveConnected = useStore((s) => s.serveConnected)
  const xiheConfig = useStore((s) => s.xiheConfig)
  const xiheStatus = useStore((s) => s.xiheStatus)
  const setTab = useStore((s) => s.setTab)
  const [input, setInput] = useState('')
  // User-message edit: the bubble in edit mode (editingId) swaps to a
  // textarea; confirm rolls the conversation back to before it and sends the
  // edited text as a fresh turn.
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  // Message whose copy click just succeeded — swaps Copy→Check briefly.
  const [copiedId, setCopiedId] = useState<string | null>(null)
  // Assistant messages render markdown by default; per-message escape hatch
  // back to the raw text (查看原文).
  const [rawIds, setRawIds] = useState<Set<string>>(new Set())
  const toggleRaw = useCallback((id: string) =>
    setRawIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    }), [])
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pending = messages.some((m) => m.role === 'assistant' && m.pending)

  // xihe (when serveConnected) supports steer while a turn runs. The stop
  // button shows while `running`.
  const running = pending && serveConnected

  // Don't yank the view when the user scrolls up (reading history, expanding a
  // 思考/工具 trace): auto-follow new content only while already near the
  // bottom. Opening/switching a conversation re-arms the follow.
  const stickRef = useRef(true)

  useEffect(() => {
    stickRef.current = true
    setEditingId(null)
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [agent.activeConvId])

  useEffect(() => {
    if (!stickRef.current) return
    // rAF-aligned: coalesced store flushes still fire this effect ~20×/s
    // during a stream; aligning the scroll to paint avoids stacking layout
    // reads between commits.
    const raf = requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    })
    return () => cancelAnimationFrame(raf)
  }, [messages])

  // Keystrokes die whenever focus falls on <body>: opening a conversation
  // leaves focus on the clicked row/button, and deleting one unmounts the row
  // that held focus (the "takes forever to accept input" reports). So every
  // conversation switch (post-mount runs) takes focus, and a list-length
  // change re-takes it when it dropped to <body>. The body guard keeps an
  // in-progress rename (sidebar input) or other deliberate focus from being
  // yanked; the mount runs keep the old no-grab behavior for a synced conv.
  const seenConvRef = useRef(false)
  const seenLenRef = useRef(false)
  useEffect(() => {
    const c = agent.conversations.find((x) => x.id === agent.activeConvId)
    const first = !seenConvRef.current
    seenConvRef.current = true
    if (c && (!c.synced || !first)) inputRef.current?.focus()
  }, [agent.activeConvId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const first = !seenLenRef.current
    seenLenRef.current = true
    if (!first && document.activeElement === document.body)
      inputRef.current?.focus()
  }, [agent.conversations.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-grow the composer with its content (capped by max-h-32, then it
  // scrolls inside). Without this a multi-line paste shows only the last line.
  useEffect(() => {
    const ta = inputRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }, [input, agent.activeConvId])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  // Hooks must ALL run before this point — the early return below would
  // otherwise change the hook count between renders (React crashes the tree).
  const copyMessage = useCallback(async (m: Message) => {
    if (!(await copyText(m.content))) return
    setCopiedId(m.id)
    setTimeout(() => setCopiedId((cur) => (cur === m.id ? null : cur)), 1500)
  }, [])

  // Stable callback bundles feeding the memoized rows (identity held by
  // useMemo, deps all stable store fns / agentId / convId).
  const userCbs = useMemo<UserRowCbs>(() => ({
    onStartEdit: (m) => {
      setEditingId(m.id)
      setEditText(m.content)
    },
    onEditText: setEditText,
    onConfirmEdit: (index, text) => {
      const t = text.trim()
      if (!t) return
      setEditingId(null)
      setEditText('')
      void editAndResendMessage(agent.id, index, t)
    },
    onCancelEdit: () => {
      setEditingId(null)
      setEditText('')
    },
    onCopy: (m) => void copyMessage(m),
    onResend: (index) => void resendMessage(agent.id, index),
  }), [agent.id, editAndResendMessage, resendMessage, copyMessage])

  const assistantCbs = useMemo<AssistantRowCbs>(() => ({
    onApprove: (id, approved, always) => approve(agent.id, id, approved, always),
    onLoadTrace: (anchor) => {
      const c = agent.activeConvId
      if (c) void loadTrace(c, anchor)
    },
    onToggleRaw: toggleRaw,
    onCopy: (m) => void copyMessage(m),
    onRegenerate: (index) => void regenerateMessage(agent.id, index),
    onGoSettings: () => setTab('manage'),
  }), [agent, approve, loadTrace, toggleRaw, copyMessage, regenerateMessage, setTab])

  // Early return is after the last hook — hooks above must always run.
  if (!agent.activeConvId) {
    if (xiheStatus?.state === 'not_found') {
      return (
        <div className="flex h-full flex-col items-center justify-center text-center">
          <XiheMissingCard message={xiheStatus.message} />
        </div>
      )
    }
    if (xiheConfig.api_key_set === false) {
      return (
        <div className="flex h-full flex-col items-center justify-center">
          <WelcomeCard onGoSettings={() => setTab('manage')} />
        </div>
      )
    }
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <div className="text-sm text-ink-4">还没有对话</div>
        <button
          onClick={() => newConversation(agent.id)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90"
        >
          <Plus className="h-4 w-4" />
          开始新对话
        </button>
      </div>
    )
  }

  const submit = () => {
    const text = input.trim()
    if (!text) return
    sendMessage(agent.id, text)
    setInput('')
  }

  const steerSubmit = () => {
    const text = input.trim()
    if (!text) return
    steer(agent.id, text)
    setInput('')
  }

  // While a turn runs, Enter steers it (non-interrupting redirect). Idle →
  // normal send.
  const onPrimary = () => {
    if (running) return steerSubmit()
    return submit()
  }

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} onScroll={onScroll} className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
        {messages.length === 0 && xiheConfig.api_key_set === false && (
          <div className="mt-10 flex justify-center">
            <WelcomeCard onGoSettings={() => setTab('manage')} />
          </div>
        )}
        {messages.length === 0 && xiheConfig.api_key_set !== false && (
          <div className="mt-10 text-center text-sm text-ink-4">
            向 <span className="text-ink-3">{agent.name}</span> 发送消息开始对话
            {!serveConnected && (
              <div className="mt-1 text-xs text-warning/70">xihe未连接，等待启动…</div>
            )}
          </div>
        )}
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <UserRow
              key={m.id}
              m={m}
              i={i}
              editing={editingId === m.id}
              editText={editText}
              copied={copiedId === m.id}
              disabled={pending}
              cbs={userCbs}
            />
          ) : (
            <AssistantRow
              key={m.id}
              m={m}
              i={i}
              running={running}
              pending={pending}
              isRaw={rawIds.has(m.id)}
              copied={copiedId === m.id}
              cbs={assistantCbs}
            />
          )
        )}
      </div>
      <div className="border-t border-line p-3">
        <div
          onClick={(e) => {
            // The visible input bar has padding the textarea doesn't cover, so
            // clicking the padding (most of the box) misses the field. Treat the
            // whole bar as the input: a click on the bar itself focuses it.
            // (e.target === e.currentTarget → the click landed on the padding/
            // gap, not on the textarea or a button, which handle themselves.)
            if (e.target === e.currentTarget) inputRef.current?.focus()
          }}
          className="flex items-end gap-2 rounded-xl bg-panel px-3 py-2"
        >
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={(e) => {
              // 复制消息/历史时常带尾随换行，粘贴进输入框后多出空行；去掉尾部
              // 空白，保留中间内容与行内格式。无尾随空白时走默认粘贴。
              const cd = e.clipboardData
              const text = cd ? cd.getData('text') : ''
              if (!text) return
              const cleaned = text.replace(/\s+$/, '')
              if (cleaned === text) return
              e.preventDefault()
              const ta = e.currentTarget
              const next =
                input.slice(0, ta.selectionStart ?? 0) +
                cleaned +
                input.slice(ta.selectionEnd ?? 0)
              setInput(next)
              const pos = (ta.selectionStart ?? 0) + cleaned.length
              requestAnimationFrame(() => {
                if (inputRef.current) {
                  inputRef.current.selectionStart = pos
                  inputRef.current.selectionEnd = pos
                }
              })
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                onPrimary()
              }
            }}
            rows={1}
            placeholder={
              running
                ? `给 ${agent.name} 追加指示（steer 改向，不打断）…`
                : `发给 ${agent.name}…`
            }
            className="max-h-32 flex-1 resize-none bg-transparent text-sm outline-none placeholder:text-ink-4"
          />
          {running ? (
            <div className="flex items-end gap-2">
              {input.trim() && (
                <button
                  onClick={steerSubmit}
                  title="改向当前回合（steer，不打断）"
                  className="rounded-lg bg-amber-600 p-2 text-white hover:bg-amber-500"
                >
                  <Navigation className="h-4 w-4" />
                </button>
              )}
              <button
                onClick={() => interrupt(agent.id)}
                title="停止生成"
                className="rounded-lg bg-contrast p-2 text-white hover:brightness-125"
              >
                <Square className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <button
              onClick={submit}
              disabled={!input.trim() || !serveConnected}
              title={!serveConnected ? 'xihe未连接' : undefined}
              className="rounded-lg bg-brand p-2 text-white disabled:opacity-30"
            >
              <Send className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
