import { create } from 'zustand'
import {
  connectStream,
  deleteSession,
  getAgents,
  getHealth,
  getHistory,
  truncateConversation,
  getTrace,
  listCron,
  listMcp,
  listSessions,
  listSkills,
  resetSession,
  setConversationTitle,
  type CronJobInfo,
  type McpServer,
  type SchedulerHealth,
  type ServeAgent,
  type ServeEvent,
  type ServeStream,
  type SkillInfo,
} from './lib/serveClient'
import { loadWorkspaceStore, saveWorkspaceStore } from './lib/persist'
import type { TurnUsage } from './lib/serveClient'
import {
  desktop,
  type XiheStatus,
  type XiheConfig,
  type XiheConfigPatch,
  type ThemeMode,
} from './lib/desktop'

export type EngineKind = 'xihe' | 'codebuddy'
export type AgentShape = 'process' | 'connector'
export type AgentStatus = 'online' | 'offline'

/** One conversation within an agent. `id` is the serve conv_id. */
export interface ConvMeta {
  id: string
  title: string
  /** true once this conv exists on the server (sent at least once, or seen in
   *  /sessions). An unsent "新对话" is local-only and can be removed without a
   *  server round-trip — DELETE on a conv serve never created returns
   *  deleted:false, which must not block local removal. */
  synced?: boolean
}

/** A reusable working directory the user can bind a conversation to. Desktop-only;
 *  serve is workspace-unaware. Persisted in localStorage (lib/persist). */
export interface Workspace {
  id: string
  name: string
  workdir: string
}

export interface Agent {
  id: string
  name: string
  engine: EngineKind
  shape: AgentShape
  status: AgentStatus
  model: string
  /** capability descriptor — UI branches on these flags, never on engine name */
  capabilities: string[]
  description: string
  /** where this agent's truth lives — shows the data-ownership split */
  dataRoot?: string
  /** true → sendMessage routes through `xihe serve` (WS). */
  serveBacked?: boolean
  /** conversations owned by this agent; a new one = a new conv_id */
  conversations: ConvMeta[]
  /** conversation currently shown in the chat panel; null = no conversation
   *  selected (empty list / boot) → chat shows a "start new chat" CTA. */
  activeConvId: string | null
}

export type TraceEvent =
  | { kind: 'thought'; text: string; ts?: number; by?: string }
  | { kind: 'tool'; name: string; args: string; status: 'running' | 'done' | 'interrupted'; elapsed?: number; result?: string; truncated?: boolean; ts?: number; by?: string }
  | { kind: 'steer'; text: string }

/** Approval card state on an assistant bubble — set by approval_request,
 *  settled by the user's buttons (optimistic) or approval_resolved (timeout /
 *  interrupt / reply from another client), expired when the turn ends with a
 *  card still pending. */
export interface PendingApproval {
  id: string
  name: string
  summary: string
  status: 'pending' | 'approved' | 'denied' | 'expired'
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  /** serve row id of the user row (history-synced messages only) — addresses
   * the message server-side for resend truncation. */
  serveId?: number
  pending?: boolean
  error?: boolean
  /** live approval card for this turn (at most one: approval tools are all
   *  write → sequential dispatch on the server) */
  pendingApproval?: PendingApproval
  /** Stop clicked — optimistic, shown until the turn finalises */
  stopping?: boolean
  /** Turn ended because the user stopped it (authoritative, from complete.reason) */
  interrupted?: boolean
  /** ordered tool/thought events for an assistant turn (rendered as a trace) */
  trace?: TraceEvent[]
  /** Historical turns: serve row id to lazy-fetch `trace` on expand (absent
   *  on live turns, which accumulate trace from the stream). */
  traceAnchor?: number
  /** Historical turns: tool-call count shown in the trace header before the
   *  trace is lazy-loaded (>0 → render the collapsed header). */
  toolsCount?: number
  /** Historical turns: serve reported persisted reasoning — mount the trace
   *  (with a 思考 badge) before lazy-load, so reasoning-only turns are visible. */
  hasReasoning?: boolean
  /** Token usage of this turn (live: from the complete event; history: from
   *  the persisted final assistant row) — rendered as a cost badge. */
  usage?: TurnUsage
}

interface DesktopState {
  agents: Agent[]
  selectedAgentId: string | null
  /** keyed by conv_id (not agent id) — one message list per conversation */
  sessions: Record<string, Message[]>
  serveConnected: boolean
  serveVersion: string | null
  /** Process-level status of the built-in xihe serve, pushed by main
   *  (`xihe:status`) and pulled on mount. Two-source design: this (main truth)
   *  decides WHEN to attempt a connection; `serveConnected` (WS truth) decides
   *  whether streaming actually SUCCEEDED. See applyXiheStatus for coordination. */
  xiheStatus: XiheStatus | null
  /** Desktop-wide (process-level, not per-agent) listings for the "管理" panel.
   *  Loaded lazily by loadManageData() when the panel mounts / serve connects —
   *  not fetched on every connectServe (avoid hitting 3 endpoints nobody opens). */
  mcpServers: McpServer[]
  skills: SkillInfo[]
  cronJobs: CronJobInfo[]
  schedulerHealth: SchedulerHealth | null
  /** active right-pane tab (chat vs manage/store full-window pages). Lifted
   *  into the store so a workspace-open action can land the user on the chat tab. */
  activeTab: 'chat' | 'manage' | 'store'
  /** Browser-panel visibility. Lifted into the store so the stream handler can
   *  auto-open it when the agent uses a browser tool (the snapped Chrome then
   *  appears without a manual toggle). */
  showBrowser: boolean
  /** True after the user explicitly closed the panel — suppresses auto-open
   *  for the rest of the current turn; the next sendMessage re-arms it. */
  browserAutoMuted: boolean
  setShowBrowser: (v: boolean) => void
  /** Close the panel WITHOUT muting auto-open — for non-user closings
   *  (switching conversations), where a later browser tool call in the new
   *  conversation should still auto-open the panel. */
  dismissBrowserPanel: () => void
  /** reusable working-directory entities (desktop-only, persisted) */
  workspaces: Workspace[]
  /** conv_id → workspace_id binding; the single source of truth for which
   *  workspace a conversation is bound to (kept off ConvMeta so it survives
   *  syncConversations rebuilds). Absent key = 通用/unbound. */
  convWorkspace: Record<string, string>
  /** Non-null while "inside" a workspace: the sidebar then shows that
   *  workspace's bound conversations instead of the agent list, and the file
   *  tree is shown on the right. null = the ordinary agent-centric view. */
  activeWorkspaceId: string | null
  /** Effective xihe config from ~/.xihe-agent/config.yaml (single source).
   *  Empty until hydrateXiheConfig() runs on mount; api_key is present only as
   *  api_key_set (never the plaintext). */
  xiheConfig: XiheConfig
  select: (id: string) => void
  sendMessage: (agentId: string, text: string) => void
  /** Resend a user message: truncate the conversation to before it (serve
   * rows + local state) and send its text again as a fresh turn. No-ops while
   * a turn is streaming or serve is unreachable. */
  resendMessage: (agentId: string, index: number) => Promise<void>
  /** Edit a user message and resend it: same rollback as resendMessage, but
   * the edited text replaces the original as the fresh turn. */
  editAndResendMessage: (agentId: string, index: number, newText: string) => Promise<void>
  /** Regenerate an assistant reply: keep the user message before it, truncate
   * from the turn's first row, and re-send that user message. */
  regenerateMessage: (agentId: string, index: number) => Promise<void>
  interrupt: (agentId: string) => void
  steer: (agentId: string, text: string) => void
  /** Answer the active turn's pending approval card (approve/deny buttons).
   *  always pairs with approval ("本会话不再询问" — session memory server-side). */
  approve: (agentId: string, id: string, approved: boolean, always?: boolean) => void
  newConversation: (agentId: string) => void
  selectConversation: (agentId: string, convId: string) => void
  resetConversation: (agentId: string, convId: string) => Promise<void>
  deleteConversation: (agentId: string, convId: string) => Promise<void>
  /** Rename a conversation. serve-backed + already on the server → POST /title
   *  (persists; xihe's auto-title skips sessions that already have a title, so a
   *  manual rename sticks). An unsent "新对话" (synced !== true) has no serve
   *  session yet → skip the round-trip and rename locally only (its first send
   *  will create the session and run auto-title, which may overwrite — an
   *  accepted edge case). */
  renameConversation: (agentId: string, convId: string, title: string) => Promise<void>
  /** Reload a conversation's transcript from serve + refresh the list (picks up
   *  serve-generated titles). `convId` targets a specific conversation (the
   *  per-conv refresh button — opens it + reloads); omit for the active one. */
  refreshConversation: (agentId: string, convId?: string) => Promise<void>
  setTab: (tab: 'chat' | 'manage' | 'store') => void
  addWorkspace: (workdir: string, name?: string) => void
  removeWorkspace: (id: string) => void
  bindWorkspaceToConv: (convId: string, workspaceId: string | null) => void
  openConversationInWorkspace: (workspaceId: string) => void
  /** Enter a workspace: scope the sidebar to its conversations, pick the most
   *  recent bound one (or seed a fresh bound conv if it has none), land on the
   *  chat tab. The middle/right panes follow because the active conv is bound. */
  openWorkspace: (workspaceId: string) => void
  /** Leave workspace view; the sidebar returns to the agent-centric list. */
  exitWorkspace: () => void
  hydrateWorkspaceStore: () => Promise<void>
  /** Read effective xihe config from ~/.xihe-agent/config.yaml into state. */
  hydrateXiheConfig: () => Promise<void>
  /** Desktop theme mode. 'dark' until hydrateTheme() reads the persisted
   *  value (~/.xihe-desktop/settings.json — desktop-local, never xihe
   *  config). The html.light class toggle driven by it lives in App.tsx. */
  theme: ThemeMode
  hydrateTheme: () => Promise<void>
  /** Apply + persist a theme immediately (click-to-apply; main flips
   *  nativeTheme and pushes the resolved light/dark to xihe so the browser
   *  panel's Chrome matches). */
  setTheme: (t: ThemeMode) => Promise<void>
  /** Line-patch xihe config.yaml with the given keys (comment-preserving), then
   *  re-read effective values into state. Returns whether the write succeeded.
   *  Edits take effect on the running serve only after serveRestart(). */
  saveXiheConfig: (patch: XiheConfigPatch) => Promise<boolean>
  connectServe: () => Promise<boolean>
  /** Apply a process-level status update from main. running → drive connectServe
   *  (the real connect trigger now); stopped/errored → drop serveConnected and
   *  stop the WS client from racing main's restart. */
  applyXiheStatus: (s: XiheStatus) => void
  /** Pull MCP/skills/cron listings from serve into state (no-op unless serve is
   *  connected). Called on ManagePanel mount + when serve connects; also wired
   *  to the panel's refresh button. */
  loadManageData: () => Promise<void>
  /** Lazy-load a historical turn's tool-call trace from serve by its anchor
   *  row id, writing it onto the message so the trace panel can render it.
   *  No-op if the message isn't found or already has a trace. */
  loadTrace: (convId: string, msgId: number) => Promise<void>
}

// [[0026]]: Agent = TYPE. xihe is the single BUILT-IN agent; claude is reached
// via xihe's external_agent tool, not as a peer agent.
const LIVE_SLOT_ID = 'xihe'

/** Resident transcript cache ceiling — beyond this, oldest non-active convs are
 *  evicted from `sessions` (their transcript refetches lazily on reopen). */
const MAX_SESSIONS = 16

const SEED_AGENTS: Agent[] = [
  {
    id: 'xihe',
    name: 'xihe',
    engine: 'xihe',
    shape: 'process',
    status: 'offline',
    model: 'glm-5.2-zp',
    capabilities: ['shell', 'browser', 'mcp', 'interrupt', 'escalation'],
    description: '内置xihe agent。',
    dataRoot: '.xihe-agent/',
    conversations: [],
    activeConvId: null,
  },
]

export const useStore = create<DesktopState>()((set, get) => {
  let stream: ServeStream | null = null
  let deliberateClose = false
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  // Guards connectServe against concurrent re-entry: the xihe:status pull and
  // push can both fire on the same `running` transition, and App mount calls us
  // once too. serveConnected alone can't dedup (it flips only after the async
  // getHealth resolves), so an in-flight flag is needed.
  let connectInFlight = false

  // Delta coalescing: text/thought deltas arrive once per model chunk — one
  // zustand set + one render per chunk made long turns quadratic (every delta
  // re-rendered the transcript). Buffer per conv, apply as ONE patch per
  // ~50ms; any non-delta event flushes first so trace ordering holds. While
  // the chat tab is hidden the flush is postponed (nothing paints) and
  // complete/error still force-flush.
  type DeltaOp = (m: Message) => Message
  const deltaBuf = new Map<string, DeltaOp[]>()
  let deltaTimer: ReturnType<typeof setTimeout> | null = null

  function bufferDelta(convId: string | undefined, op: DeltaOp) {
    if (!convId) return
    const ops = deltaBuf.get(convId)
    if (ops) ops.push(op)
    else deltaBuf.set(convId, [op])
    scheduleDeltaFlush()
  }

  function scheduleDeltaFlush() {
    if (deltaTimer) return
    deltaTimer = setTimeout(() => {
      deltaTimer = null
      if (deltaBuf.size === 0) return
      if (get().activeTab !== 'chat') {
        setTimeout(scheduleDeltaFlush, 200)
        return
      }
      flushDeltas()
    }, 50)
  }

  /** Apply every conv's buffered delta ops as one message patch + one set. */
  function flushDeltas() {
    if (deltaBuf.size === 0) return
    for (const [convId, ops] of [...deltaBuf]) {
      deltaBuf.delete(convId)
      if (ops.length === 0) continue
      patchPending(convId, (m) => ops.reduce((mm, op) => op(mm), m))
    }
  }

  /** Keep the resident transcript cache bounded: evict the oldest
   * non-protected conv entries (whole key — reopening lazily refetches from
   * serve, `convId in sessions` is the cache sentinel). Protected: every
   * agent's active conv and any conv with a live turn. */
  function trimSessions() {
    const s = get()
    const keys = Object.keys(s.sessions)
    if (keys.length <= MAX_SESSIONS) return
    const protect = new Set(s.agents.map((a) => a.activeConvId ?? ''))
    for (const k of keys) {
      if ((s.sessions[k] ?? []).some((m) => m.role === 'assistant' && m.pending))
        protect.add(k)
    }
    let excess = keys.length - MAX_SESSIONS
    const sessions = { ...s.sessions }
    for (const k of keys) {
      if (excess <= 0) break
      if (protect.has(k)) continue
      delete sessions[k]
      excess--
    }
    if (Object.keys(sessions).length !== keys.length) set({ sessions })
  }

  function scheduleReconnect() {
    if (reconnectTimer || deliberateClose) return
    // Don't hammer /stream while main reports the serve process is down (or
    // still starting) — main owns the restart, and reconnecting would race it.
    // Only retry transient socket blips that happen WHILE running.
    const st = get().xiheStatus?.state
    if (st && st !== 'running') return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      void openStream()
    }, 3000)
  }

  async function openStream() {
    if (deliberateClose) return
    try {
      stream = await connectStream(handleEvent, (connected) => {
        set({ serveConnected: connected })
        if (!connected && !deliberateClose) scheduleReconnect()
      })
      await resyncAfterReconnect()
    } catch {
      if (!deliberateClose) scheduleReconnect()
    }
  }

  /** After (re)connecting: a turn that survived a dropout server-side (serve
   *  keeps detached turns running for a grace window) resumes streaming to
   *  this socket via `attach`. The transcript refetch self-skips while a turn
   *  streams locally (refetchConv's guard — wiping the live bubble was the
   *  missing-正在思考… bug); a turn that COMPLETED while we were away is
   *  settled by the attached{running:false} ack's force-refetch instead. */
  async function resyncAfterReconnect() {
    const a = get().agents.find((x) => x.id === LIVE_SLOT_ID)
    const convId = a?.activeConvId
    if (!a?.serveBacked || !convId || !get().serveConnected) return
    stream?.attach(convId)
    await refetchConv(convId)
  }

  function handleEvent(e: ServeEvent) {
    // Non-delta events flush any buffered deltas first — a tool_call/complete
    // must observe the text that streamed before it.
    if (e.type !== 'text_delta' && e.type !== 'thought_delta') flushDeltas()
    if (e.type === 'text_delta')
      bufferDelta(e.conv_id, (m) => ({ ...m, content: m.content + e.text, pending: true }))
    else if (e.type === 'thought_delta')
      // Accumulate consecutive reasoning deltas into one thought entry — but
      // only within the same source (`by`): claude's thinking must not glue
      // onto the main agent's mid-stream.
      bufferDelta(e.conv_id, (m) => {
        const trace = m.trace ? m.trace.slice() : []
        const last = trace[trace.length - 1]
        if (last && last.kind === 'thought' && last.by === e.by)
          trace[trace.length - 1] = { ...last, text: last.text + e.text }
        else trace.push({ kind: 'thought', text: e.text, ts: Date.now(), by: e.by })
        return { ...m, trace }
      })
    else if (e.type === 'tool_call') {
      // Agent picked up a browser tool → surface the panel so the snapped
      // Chrome is visible without a manual toggle (unless the user closed it).
      if (e.name.startsWith('browser_') && !get().browserAutoMuted) {
        set({ showBrowser: true })
      }
      patchPending(e.conv_id, (m) => {
        const trace = m.trace ? m.trace.slice() : []
        trace.push({ kind: 'tool', name: e.name, args: e.args, status: 'running', ts: Date.now(), by: e.by })
        return { ...m, trace }
      })
    }
    else if (e.type === 'tool_result')
      // No call id in the protocol → match the earliest running tool with the
      // same name (FIFO), preferring the same source (`by`) so main-agent and
      // child tools sharing a name don't cross-pair. Best-effort for parallel
      // same-name tools (display only).
      patchPending(e.conv_id, (m) => {
        if (!m.trace) return m
        const trace = m.trace.slice()
        const isRunningNamed = (by?: string) => (t: TraceEvent) =>
          t.kind === 'tool' && t.name === e.name && t.status === 'running' && t.by === by
        let idx = trace.findIndex(isRunningNamed(e.by))
        if (idx < 0) idx = trace.findIndex(isRunningNamed(undefined))
        const cur = idx >= 0 ? trace[idx] : undefined
        if (cur && cur.kind === 'tool')
          trace[idx] = { ...cur, status: 'done', elapsed: e.elapsed, result: e.result, truncated: e.truncated }
        return { ...m, trace }
      })
    else if (e.type === 'turn_start') {
      // No-op: the pending assistant bubble is created optimistically in
      // sendMessage. Interrupt targets conv_id, not turn_id.
    } else if (e.type === 'attached') {
      // serve's attach ack. running=false with a local pending bubble → the
      // turn settled server-side while we were disconnected; no complete is
      // coming, so force-refetch to replace the stale bubble with the
      // persisted reply (refetchConv's normal guard would skip it). Without a
      // pending bubble the resync refetch already covers staleness.
      if (!e.running) {
        const live = (get().sessions[e.conv_id] ?? []).some(
          (m) => m.role === 'assistant' && m.pending
        )
        if (live) void refetchConv(e.conv_id, true)
      }
    } else if (e.type === 'approval_request') {
      patchPending(e.conv_id, (m) => ({
        ...m,
        pendingApproval: { id: e.id, name: e.name, summary: e.summary, status: 'pending' },
      }))
    } else if (e.type === 'approval_resolved') {
      // Every resolution path lands here — the other client's reply, timeout,
      // interrupt. Our own button click already settled the card optimistically;
      // this is authoritative for everything else.
      patchPending(e.conv_id, (m) => {
        const ap = m.pendingApproval
        if (!ap || ap.id !== e.id || ap.status !== 'pending') return m
        return {
          ...m,
          pendingApproval: {
            ...ap,
            status: e.approved ? 'approved' : 'denied',
          },
        }
      })
    } else if (e.type === 'complete') {
      const interrupted = e.reason === 'interrupted'
      // chat() returns "API error: …" as text instead of raising — flag it so
      // the bubble renders as an error, not a normal reply.
      const failed = e.reason === 'api_error' || e.reason === 'api_timeout'
      patchPending(e.conv_id, (m) => ({
        ...m,
        // On interrupt keep the partial text already streamed; don't overwrite
        // it with the agent's "[interrupted]" marker.
        content: interrupted ? m.content : e.text || m.content,
        pending: false,
        stopping: undefined,
        interrupted: interrupted || undefined,
        error: failed || undefined,
        usage: e.usage,
        pendingApproval: expireApproval(m.pendingApproval),
        trace: sweepRunningTools(m.trace, interrupted ? 'interrupted' : 'done'),
      }))
      const owner = owningAgent(e.conv_id)
      if (owner?.serveBacked && get().serveConnected) {
        // Refresh the conversation list so a serve-generated title (first turn)
        // appears and the list re-sorts by recency. syncConversations never
        // touches messages, so the just-finalised content is safe.
        void syncConversations(owner.id)
        // serve generates the session title on a fire-and-forget aux-LLM thread
        // started inside agent.chat, which lands AFTER `complete` — so the
        // sync above usually reads the row before its title is written. Re-sync
        // a moment later to pick up the generated title (manual refresh covers
        // the slow-aux tail).
        setTimeout(() => {
          if (get().serveConnected) void syncConversations(owner.id)
        }, 2500)
      }
      trimSessions()
    } else if (e.type === 'error') {
      patchPending(e.conv_id, (m) => ({
        ...m,
        content: `⚠️ ${e.message}`,
        pending: false,
        stopping: undefined,
        error: true,
        pendingApproval: expireApproval(m.pendingApproval),
        trace: sweepRunningTools(m.trace),
      }))
    }
    // hello: connection metadata, already applied via connectServe
  }

  /** Routes a streaming event to the pending assistant bubble of its conv_id.
   * Events already carry conv_id, so we index sessions directly (no agent
   * reverse-lookup needed). */
  function patchPending(convId: string | undefined, fn: (m: Message) => Message) {
    if (!convId) return
    const list = get().sessions[convId] ?? []
    let idx = -1
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].role === 'assistant' && list[i].pending) {
        idx = i
        break
      }
    }
    // No pending bubble — normally events can't precede the optimistic bubble
    // sendMessage creates, but after a reconnect resync replaced the
    // transcript, resumed deltas arrive with no bubble to land on. Create one.
    if (idx < 0) {
      const fresh: Message = { id: uid(), role: 'assistant', content: '', pending: true }
      set((s) => ({
        sessions: { ...s.sessions, [convId]: [...list, fn(fresh)] },
      }))
      return
    }
    const next = list.slice()
    next[idx] = fn(next[idx])
    set((s) => ({ sessions: { ...s.sessions, [convId]: next } }))
  }

  /** Which agent owns a conv_id (its conversation list contains it). conv_id may
   *  be undefined for some event variants (e.g. `error`) → returns undefined. */
  function owningAgent(convId: string | undefined): Agent | undefined {
    if (!convId) return undefined
    return get().agents.find((a) => a.conversations.some((c) => c.id === convId))
  }

  /** Rebuild an agent's conversation list from serve `/sessions`: server rows
   *  update title/order; local unsent new conversations (not yet on the server)
   *  are preserved at the top. Never touches messages. */
  async function syncConversations(agentId: string) {
    const a = get().agents.find((x) => x.id === agentId)
    if (!a?.serveBacked || !get().serveConnected) return
    const rows = await listSessions()
    const serverIds = new Set(rows.map((r) => r.conv_id))
    const localOnly = a.conversations.filter((c) => !serverIds.has(c.id))
    // localOnly was snapshotted before the await; a sync/new-conv during it can
    // resurface a stale local copy beside its server copy, so dedupe.
    const merged: ConvMeta[] = []
    const seen = new Set<string>()
    for (const r of rows) {
      if (seen.has(r.conv_id)) continue
      seen.add(r.conv_id)
      merged.push({ id: r.conv_id, title: r.title || '新对话', synced: true })
    }
    for (const c of localOnly) {
      if (seen.has(c.id)) continue
      seen.add(c.id)
      merged.push(c)
    }
    // Nothing selected yet (fresh boot, or the user just deleted the last conv)
    // and the server has history → land on the most recent so the chat isn't an
    // empty CTA despite a populated list. Self-limiting: once a conv is active
    // this never overrides the user's selection.
    const pickActive = a.activeConvId ?? (merged.length > 0 ? merged[0].id : null)
    set((s) => ({
      agents: s.agents.map((x) =>
        x.id === agentId ? { ...x, conversations: merged, activeConvId: pickActive } : x
      ),
    }))
  }

  /** Fetch + map a conversation's transcript from serve into Message[]. Shared
   *  by lazy first-load (loadActiveHistory) and forced reload (refreshConversation). */
  async function fetchHistory(convId: string): Promise<Message[]> {
    const msgs = await getHistory(convId)
    return msgs
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({
        id: uid(),
        role: m.role as 'user' | 'assistant',
        content: m.content,
        serveId: m.role === 'user' && typeof m.id === 'number' ? m.id : undefined,
        // Assistant turns fold their tool calls into a count + a stable anchor
        // (serve row id); the trace itself is lazy-fetched on expand.
        traceAnchor: typeof m.id === 'number' ? m.id : undefined,
        toolsCount: typeof m.tools === 'number' ? m.tools : undefined,
        hasReasoning: m.has_reasoning === true,
        usage: m.usage,
      }))
  }

  /** Replace a conv's transcript with freshly fetched history. Skips while a
   *  turn is streaming — a replace would wipe the in-flight bubble (its
   *  正在思考… hint plus any already-streamed text) and leave a blank gap
   *  until the next delta auto-recreates one. `force` bypasses the guard for
   *  a turn serve confirmed already settled (the attached{running:false}
   *  ack): the stale bubble must be replaced by the persisted final text. */
  async function refetchConv(convId: string, force = false) {
    const streaming = () =>
      (get().sessions[convId] ?? []).some((m) => m.role === 'assistant' && m.pending)
    if (!force && streaming()) return
    const mapped = await fetchHistory(convId)
    // Re-check after the await: a send that started mid-fetch must not have
    // its optimistic bubble wiped by this now-stale snapshot.
    if (!force && streaming()) return
    set((s) => ({ sessions: { ...s.sessions, [convId]: mapped } }))
  }

  /** Lazy-load persisted history from serve for the agent's active conversation
   *  (first open only — skips if already loaded). */
  async function loadActiveHistory(agentId: string) {
    const a = get().agents.find((x) => x.id === agentId)
    if (!a?.serveBacked || !get().serveConnected) return
    const convId = a.activeConvId
    if (!convId || convId in get().sessions) return
    const mapped = await fetchHistory(convId)
    set((s) => ({ sessions: { ...s.sessions, [convId]: mapped } }))
  }

  /** Mirror workspaces + bindings to ~/.xihe-desktop/workspaces.json. Called
   *  after every mutation; fire-and-forget — state is the UI's source of truth,
   *  the file is the persisted copy. */
  function persistWs(): void {
    const s = get()
    void saveWorkspaceStore({ workspaces: s.workspaces, convWorkspace: s.convWorkspace })
  }

  /** Shared rollback for 重新发送/编辑重发: pull a fresh transcript, truncate
   *  the conversation at the user bubble addressed by `index`, adopt the
   *  post-truncate truth, then send `text` (null = re-send the row's own
   *  content). No-op while a turn is streaming or serve is unreachable. */
  async function rollbackAndSend(agentId: string, index: number, text: string | null) {
    const a = get().agents.find((x) => x.id === agentId)
    const convId = a?.activeConvId
    if (!a || !convId || !a.serveBacked || !get().serveConnected) return
    if ((get().sessions[convId] ?? []).some((m) => m.role === 'assistant' && m.pending))
      return
    const mapped = await fetchHistory(convId)
    const target = mapped[index]
    if (!target || target.role !== 'user' || target.serveId == null) return
    const ok = await truncateConversation(convId, target.serveId)
    if (!ok) return
    // Adopt the post-truncate truth (everything before the target), then
    // let the normal send path append + stream the fresh turn.
    set((s) => ({ sessions: { ...s.sessions, [convId]: mapped.slice(0, index) } }))
    get().sendMessage(agentId, text ?? target.content)
  }

  return {
    agents: SEED_AGENTS,
    selectedAgentId: SEED_AGENTS[0].id,
    sessions: {},
    serveConnected: false,
    serveVersion: null,
    // null until the first xihe:status push/pull lands (main starts the serve
    // child on app boot and reports back within ~1s).
    xiheStatus: null,
    // Empty until loadManageData() runs (ManagePanel mount / serve connect).
    mcpServers: [],
    skills: [],
    cronJobs: [],
    schedulerHealth: null,
    activeTab: 'chat',
    showBrowser: false,
    browserAutoMuted: false,
    // Empty until hydrateWorkspaceStore() loads ~/.xihe-desktop/workspaces.json
    // on app mount (file IO is async, so the store can't seed synchronously).
    workspaces: [],
    convWorkspace: {},
    activeWorkspaceId: null,
    // Empty until hydrateXiheConfig() reads ~/.xihe-agent/config.yaml on mount
    // (file IO is async). Keys present here are EFFECTIVE values (file value or
    // xihe's default); api_key only as api_key_set.
    xiheConfig: {},
    // 'dark' both as the persisted default and pre-hydrate stand-in, so the
    // first paint already matches the last session's likely look.
    theme: 'dark',

    hydrateTheme: async () => {
      const s = await desktop.settingsLoad()
      set({ theme: s.theme })
    },
    setTheme: async (t) => {
      set({ theme: t })
      await desktop.setTheme(t)
    },

    select: (id) => {
      // Picking an agent means leaving any workspace view (the sidebar flips
      // back to the agent-centric conversation list).
      set({ selectedAgentId: id, activeWorkspaceId: null })
      const a = get().agents.find((x) => x.id === id)
      // serve-backed + connected → keep the conversation list fresh and load
      // the active conversation's history on first open.
      if (a?.serveBacked && get().serveConnected) {
        // sync first (it may auto-select a conv when none is active), THEN load
        // that conv's history — firing both concurrently makes load bail on the
        // pre-sync null activeConvId and leaves the just-selected conv empty.
        void syncConversations(id).then(() => loadActiveHistory(id))
      }
    },

    sendMessage: (agentId, text) => {
      const agent = get().agents.find((a) => a.id === agentId)
      const convId = agent?.activeConvId
      if (!convId || !agent) return

      const prev = get().sessions[convId] ?? []
      const userMsg: Message = { id: uid(), role: 'user', content: text }
      const pending: Message = { id: uid(), role: 'assistant', content: '', pending: true }
      // A fresh turn re-arms browser auto-open (the previous turn's explicit
      // close only mutes within that turn).
      set((s) => ({
        sessions: { ...s.sessions, [convId]: [...prev, userMsg, pending] },
        browserAutoMuted: false,
      }))

      if (agent?.serveBacked && get().serveConnected && stream) {
        // Resolve the conversation's bound workspace workdir so serve threads it
        // into the agent as cwd (relative paths + terminal then land in the
        // workspace). Unbound conv → undefined → frame carries no cwd → serve
        // falls back to its process cwd (unchanged for 通用 conversations).
        const wsId = convId ? get().convWorkspace[convId] : undefined
        const workdir = wsId
          ? get().workspaces.find((w) => w.id === wsId)?.workdir
          : undefined
        stream.sendTurn(convId, text, workdir)
        // First send creates the session server-side → mark synced so a later
        // delete knows to delete it there (not just locally).
        set((s) => ({
          agents: s.agents.map((a) =>
            a.id === agentId
              ? {
                  ...a,
                  conversations: a.conversations.map((c) =>
                    c.id === convId ? { ...c, synced: true } : c
                  ),
                }
              : a
          ),
        }))
        return
      }
      // Not connected to serve — no mock fallback. Mark the pending bubble as
      // an error so the user sees the message didn't go through.
      set((s) => ({
        sessions: {
          ...s.sessions,
          [convId]: (s.sessions[convId] ?? []).map((m) =>
            m.id === pending.id
              ? {
                  ...m,
                  content: '⚠️ xihe未连接，无法发送。请在「设置」页检查连接后重试。',
                  pending: false,
                  error: true,
                }
              : m
          ),
        },
      }))
    },

    // Live-sent messages carry no serve row id, so both rollback paths re-pull
    // the transcript before addressing the target server-side (indexes match
    // 1:1: the local list is either built from this same reshape or mirrors it).
    resendMessage: (agentId, index) => rollbackAndSend(agentId, index, null),

    editAndResendMessage: async (agentId, index, newText) => {
      const t = newText.trim()
      if (!t) return
      await rollbackAndSend(agentId, index, t)
    },

    regenerateMessage: async (agentId, index) => {
      // Same rollback as 重新发送, addressed at the prompt row: truncate AT
      // the user message (deleting it together with the reply), then re-send
      // its content. The old shape truncated at the assistant anchor — the
      // user row survived server-side while sendMessage re-sent the same
      // text, leaving the message duplicated locally AND in sessions.db.
      await rollbackAndSend(agentId, index - 1, null)
    },

    interrupt: (agentId) => {
      const a = get().agents.find((x) => x.id === agentId)
      const convId = a?.activeConvId
      if (!convId) return
      if (!stream) return
      stream.interrupt(convId)
      // Optimistic: show "正在停止…" right away so the click feels responsive.
      // Authoritative state arrives with `complete` (interrupted badge if the
      // turn really stopped, normal finalize if it finished first).
      patchPending(convId, (m) => ({ ...m, stopping: true }))
    },

    steer: (agentId, text) => {
      const a = get().agents.find((x) => x.id === agentId)
      const convId = a?.activeConvId
      if (!convId) return
      if (!stream) return
      stream.steer(convId, text)
      // serve injects the steer into the running turn silently (no WS event
      // back), so echo it into the turn's trace so the user sees their redirect
      // landed — in arrival order relative to tools/thoughts.
      patchPending(convId, (m) => {
        const trace = m.trace ? m.trace.slice() : []
        trace.push({ kind: 'steer', text })
        return { ...m, trace }
      })
    },

    approve: (agentId, id, approved, always) => {
      const a = get().agents.find((x) => x.id === agentId)
      const convId = a?.activeConvId
      if (!convId) return
      if (!stream) return
      stream.approve(convId, id, approved, always)
      // Optimistic settle so the buttons react instantly; the server's
      // approval_resolved for our own click is a no-op (status already set).
      patchPending(convId, (m) => {
        const ap = m.pendingApproval
        if (!ap || ap.id !== id || ap.status !== 'pending') return m
        return { ...m, pendingApproval: { ...ap, status: approved ? 'approved' : 'denied' } }
      })
    },

    newConversation: (agentId) => {
      const a = get().agents.find((x) => x.id === agentId)
      // Reuse the active conversation if it's still a blank, unsent "新对话" —
      // avoids a stack of identical empty entries from repeated clicks.
      const cur = a?.conversations.find((c) => c.id === a.activeConvId)
      if (cur && !cur.synced && (get().sessions[cur.id] ?? []).length === 0) return
      const convId = 'desktop-' + uid()
      set((s) => ({
        agents: s.agents.map((a) =>
          a.id === agentId
            ? {
                ...a,
                conversations: [{ id: convId, title: '新对话' }, ...a.conversations],
                activeConvId: convId,
              }
            : a
        ),
        sessions: { ...s.sessions, [convId]: [] },
      }))
      trimSessions()
    },

    selectConversation: (agentId, convId) => {
      set((s) => ({
        selectedAgentId: agentId,
        agents: s.agents.map((a) => (a.id === agentId ? { ...a, activeConvId: convId } : a)),
      }))
      // Lazy-load history for the newly-selected conversation on first open.
      void loadActiveHistory(agentId)
    },

    resetConversation: async (agentId, convId) => {
      const a = get().agents.find((x) => x.id === agentId)
      // serve-backed: reset server-side (starts a fresh session, same conv_id).
      // Offline: just clear the local messages.
      if (a?.serveBacked && get().serveConnected) {
        const ok = await resetSession(convId)
        if (!ok) return
      }
      set((s) => ({
        sessions: { ...s.sessions, [convId]: [] },
        agents: s.agents.map((x) =>
          x.id === agentId
            ? {
                ...x,
                conversations: x.conversations.map((c) =>
                  c.id === convId ? { ...c, title: '新对话' } : c
                ),
              }
            : x
        ),
      }))
    },

    deleteConversation: async (agentId, convId) => {
      const a = get().agents.find((x) => x.id === agentId)
      // If the live conv has a running turn, stop it first — otherwise the
      // turn keeps persisting and get_or_create_session would resurrect the
      // session we just deleted on its next write.
      if (convId === a?.activeConvId) {
        const list = get().sessions[convId] ?? []
        if (list.some((m) => m.role === 'assistant' && m.pending) && stream) {
          stream.interrupt(convId)
        }
      }
      const conv = a?.conversations.find((c) => c.id === convId)
      // Server-backed AND already on the server (sent once / seen in /sessions)
      // → delete server-side, bail on real failure. An unsent "新对话" never
      // reached serve, so remove it locally without a round-trip — otherwise
      // DELETE returns deleted:false and the entry gets stuck undeletable.
      if (a?.serveBacked && get().serveConnected && conv?.synced) {
        const ok = await deleteSession(convId)
        if (!ok) return
      }
      set((s) => {
        const agent = s.agents.find((x) => x.id === agentId)
        if (!agent) return {}
        const remaining = agent.conversations.filter((c) => c.id !== convId)
        const sessions = { ...s.sessions }
        delete sessions[convId]
        // Deleted the active one → fall back to the next conversation, or null
        // when that was the last one (the chat panel then shows its "start new
        // chat" empty state instead of forcing a phantom 新对话 back into
        // existence — the undeletable-conversation bug).
        const conversations = remaining
        const activeConvId =
          convId === agent.activeConvId
            ? remaining.length > 0
              ? remaining[0].id
              : null
            : agent.activeConvId
        return {
          sessions,
          agents: s.agents.map((x) =>
            x.id === agentId ? { ...x, conversations, activeConvId } : x
          ),
        }
      })
      // Deleted the active conversation → the fallback selection was never
      // opened this session; lazy-load its history like selectConversation
      // does, else it renders blank until manually re-selected.
      if (convId === a?.activeConvId) void loadActiveHistory(agentId)
    },

    renameConversation: async (agentId, convId, title) => {
      const t = title.trim()
      if (!t) return
      const a = get().agents.find((x) => x.id === agentId)
      const conv = a?.conversations.find((c) => c.id === convId)
      // serve-backed AND already on the server → persist the rename there.
      // xihe's auto-title skips sessions that already have a title, so once this
      // lands the manual rename won't be overwritten. An unsent "新对话" has no
      // session yet → skip the POST (it 404s) and keep the name locally; its
      // first send creates the session and may auto-title over it (edge case).
      if (a?.serveBacked && get().serveConnected && conv?.synced) {
        const ok = await setConversationTitle(convId, t)
        if (!ok) return
      }
      set((s) => ({
        agents: s.agents.map((x) =>
          x.id === agentId
            ? {
                ...x,
                conversations: x.conversations.map((c) =>
                  c.id === convId ? { ...c, title: t } : c
                ),
              }
            : x
        ),
      }))
    },

    // Reload the active conversation from serve: re-pull its transcript (so the
  // chat reflects server truth, e.g. after an external edit) AND refresh the
  // conversation list (which is how serve-generated titles sync back). Skips
  // the message reload while a turn is streaming — it would drop the in-flight
  // assistant bubble whose deltas are still arriving.
  refreshConversation: async (agentId, convId) => {
    const a = get().agents.find((x) => x.id === agentId)
    if (!a?.serveBacked || !get().serveConnected) return
    // Target the given conversation (per-conv refresh button) or fall back to
    // the active one (top refresh button).
    const target = convId ?? a.activeConvId
    if (!target) return
    // Open the target if it isn't already active. selectConversation leaves the
    // workspace view intact (it doesn't touch activeWorkspaceId), so refreshing
    // a conv from inside a workspace doesn't kick you out of it.
    if (target !== a.activeConvId) get().selectConversation(agentId, target)
    // Force-reload its transcript from serve (overrides the lazy-load cache);
    // refetchConv skips while a turn is streaming.
    await refetchConv(target)
    // Refresh the list too — this is how serve-generated titles sync back.
    await syncConversations(agentId)
  },

    setTab: (tab) => {
      set({ activeTab: tab })
      // Returning to the chat tab: catch up any deltas buffered while hidden.
      if (tab === 'chat') flushDeltas()
    },

    // Opening (by click or auto) re-arms auto-open; closing mutes it for the
    // rest of the current turn so a running agent's later browser calls don't
    // fight the user's explicit close.
    setShowBrowser: (v) =>
      set(
        v
          ? { showBrowser: true, browserAutoMuted: false }
          : { showBrowser: false, browserAutoMuted: true }
      ),

    dismissBrowserPanel: () => set({ showBrowser: false }),

    addWorkspace: (workdir, name) => {
      // Default name = last path segment (pure JS; the renderer can't use
      // node's `path`). User-supplied name wins if non-empty.
      const seg = workdir.split(/[\\/]/).filter(Boolean).pop()
      const ws: Workspace = {
        id: 'ws-' + uid(),
        name: name?.trim() || seg || workdir,
        workdir,
      }
      set((s) => ({ workspaces: [...s.workspaces, ws] }))
      persistWs()
    },

    removeWorkspace: (id) => {
      set((s) => {
        const workspaces = s.workspaces.filter((w) => w.id !== id)
        // Drop bindings to the removed workspace; bound conversations are NOT
        // deleted — they fall back to 通用 (the selector returns undefined →
        // chip/tree vanish on their own).
        const convWorkspace: Record<string, string> = {}
        for (const [cid, wid] of Object.entries(s.convWorkspace)) {
          if (wid !== id) convWorkspace[cid] = wid
        }
        return { workspaces, convWorkspace }
      })
      persistWs()
    },

    bindWorkspaceToConv: (convId, workspaceId) => {
      set((s) => {
        const convWorkspace = { ...s.convWorkspace }
        if (workspaceId) convWorkspace[convId] = workspaceId
        else delete convWorkspace[convId]
        return { convWorkspace }
      })
      persistWs()
    },

    // Advanced path: always open a FRESH conversation (under the serve-backed
    // xihe slot) bound to this workspace, and land on the chat tab. Unlike
    // newConversation it always binds — this is the workspace fast path for
    // advanced users (also the "新对话" button inside workspace view).
    openConversationInWorkspace: (workspaceId) => {
      if (!get().workspaces.some((w) => w.id === workspaceId)) return
      // Reuse the active conv if it's still a blank, unsent "新对话" already
      // bound to this workspace — avoids a stack of empty entries from repeated
      // clicks in workspace view.
      const a0 = get().agents.find((x) => x.id === LIVE_SLOT_ID)
      const cur = a0?.conversations.find((c) => c.id === a0.activeConvId)
      if (
        cur &&
        !cur.synced &&
        (get().sessions[cur.id] ?? []).length === 0 &&
        get().convWorkspace[cur.id] === workspaceId
      ) {
        return
      }
      const convId = 'desktop-' + uid()
      set((s) => ({
        selectedAgentId: LIVE_SLOT_ID,
        activeTab: 'chat',
        convWorkspace: { ...s.convWorkspace, [convId]: workspaceId },
        sessions: { ...s.sessions, [convId]: [] },
        agents: s.agents.map((a) =>
          a.id === LIVE_SLOT_ID
            ? {
                ...a,
                conversations: [{ id: convId, title: '新对话' }, ...a.conversations],
                activeConvId: convId,
              }
            : a
        ),
      }))
      persistWs()
    },

    // Enter a workspace: the sidebar becomes scoped to this workspace's
    // conversations (left = history), the active conv drives the chat (middle)
    // and its binding drives the tree (right). Select the most recent bound
    // conversation; if there is none yet, seed a fresh bound one so the middle
    // pane isn't empty.
    openWorkspace: (workspaceId) => {
      if (!get().workspaces.some((w) => w.id === workspaceId)) return
      const a = get().agents.find((x) => x.id === LIVE_SLOT_ID)
      const bound = a
        ? a.conversations.filter((c) => get().convWorkspace[c.id] === workspaceId)
        : []
      if (bound.length > 0) {
        const convId = bound[0].id
        set((s) => ({
          selectedAgentId: LIVE_SLOT_ID,
          activeWorkspaceId: workspaceId,
          activeTab: 'chat',
          agents: s.agents.map((x) =>
            x.id === LIVE_SLOT_ID ? { ...x, activeConvId: convId } : x
          ),
        }))
      } else {
        // No history yet — seed a blank bound conversation.
        get().openConversationInWorkspace(workspaceId)
        set({ activeWorkspaceId: workspaceId })
      }
      void loadActiveHistory(LIVE_SLOT_ID)
    },

    exitWorkspace: () => set({ activeWorkspaceId: null }),

    // Load ~/.xihe-desktop/workspaces.json into state. Called once on app mount
    // (file IO is async, so the store seeds empty and fills shortly after).
    hydrateWorkspaceStore: async () => {
      const data = await loadWorkspaceStore()
      set({ workspaces: data.workspaces, convWorkspace: data.convWorkspace })
    },

    hydrateXiheConfig: async () => {
      const xiheConfig = await desktop.xiheConfigLoad()
      set({ xiheConfig })
    },

    // Patch config.yaml then re-read effective values (no secret in state —
    // api_key returns only as api_key_set). Edits apply to the RUNNING serve
    // only after serveRestart() (serve reads config at boot).
    saveXiheConfig: async (patch) => {
      const ok = await desktop.xiheConfigSave(patch)
      if (ok) {
        const xiheConfig = await desktop.xiheConfigLoad()
        set({ xiheConfig })
      }
      return ok
    },

    loadManageData: async () => {
      // No-op when serve isn't up — the panel shows empty + the effect re-fires
      // once serveConnected flips true (App calls connectServe on mount).
      if (!get().serveConnected) return
      const [mcpServers, skills, cron] = await Promise.all([
        listMcp(),
        listSkills(),
        listCron(),
      ])
      set({
        mcpServers,
        skills,
        cronJobs: cron.jobs,
        schedulerHealth: cron.scheduler,
      })
    },

    loadTrace: async (convId, msgId) => {
      // No-op if the message is gone or already has a trace (re-expand guard).
      const list = get().sessions[convId] ?? []
      const idx = list.findIndex((m) => m.traceAnchor === msgId)
      if (idx < 0 || list[idx].trace) return
      const trace = await getTrace(convId, msgId)
      set((s) => {
        const cur = s.sessions[convId] ?? []
        const i = cur.findIndex((m) => m.traceAnchor === msgId)
        if (i < 0 || cur[i].trace) return {} // raced / already loaded
        const next = cur.slice()
        next[i] = { ...next[i], trace: trace as TraceEvent[] }
        return { sessions: { ...s.sessions, [convId]: next } }
      })
    },

    applyXiheStatus: (s) => {
      set({ xiheStatus: s })
      if (s.state === 'running') {
        // main reports serve up → drive the (idempotent) streaming connect.
        // Skip when already connected (App-mount connectServe may have got there
        // first) to avoid opening a second WS.
        if (!get().serveConnected) void get().connectServe()
      } else {
        // starting | stopped | errored → not streaming. Drop serveConnected at
        // once (don't wait for the socket to notice) and arm deliberateClose so
        // any pending reconnect no-ops while main restarts; connectServe clears
        // it when `running` returns.
        if (get().serveConnected) set({ serveConnected: false })
        deliberateClose = true
      }
    },

    connectServe: async () => {
      // Idempotent vs concurrent re-entry (xihe:status pull+push + App mount).
      if (connectInFlight || get().serveConnected) return get().serveConnected
      connectInFlight = true
      try {
        const health = await getHealth()
        if (!health) {
          set({ serveConnected: false })
          return false
        }
        const remote = await getAgents()
        deliberateClose = false
        set((s) => ({
          serveConnected: true,
          serveVersion: health.version,
          agents: s.agents.map((a) => {
            if (a.id !== LIVE_SLOT_ID) return a
            const r: ServeAgent | undefined = remote[0]
            // Keep the seeded conversations/activeConvId (empty/null until
            // syncConversations fills the list); just adopt the serve-discovered
            // name/model/caps and flip serveBacked.
            if (!r)
              return { ...a, status: 'online', serveBacked: true }
            return {
              ...a,
              status: 'online',
              serveBacked: true,
              name: r.name || a.name,
              engine: 'xihe',
              shape: 'process',
              model: r.model || a.model,
              capabilities: r.capabilities?.length ? r.capabilities : a.capabilities,
              dataRoot: r.dataRoot ?? a.dataRoot,
              description: 'xihe已连接。对话历史存于本地 sessions.db。',
            }
          }),
        }))
        void openStream()
        // select() syncs the conversation list + loads history for the now-live slot.
        get().select(LIVE_SLOT_ID)
        return true
      } finally {
        connectInFlight = false
      }
    },
  }
})

/** Mark any still-running tool entries when a turn ends — an interrupt can
 * leave a tool mid-flight with no matching tool_result (avoids stuck spinners).
 * `to` controls the final status: 'done' for a normal/errored end, 'interrupted'
 * when the user stopped the turn (so the tool reads as cancelled, not ✓).
 * Returns the same array reference if nothing changed. */
function sweepRunningTools(trace?: TraceEvent[], to: 'done' | 'interrupted' = 'done'): TraceEvent[] | undefined {
  if (!trace || !trace.length) return trace
  let changed = false
  const next = trace.map((t) => {
    if (t.kind === 'tool' && t.status === 'running') {
      changed = true
      return { ...t, status: to }
    }
    return t
  })
  return changed ? next : trace
}

/** A pending approval outliving its turn (complete/error with no
 *  approval_resolved — e.g. the socket dropped) reads as unanswered. */
function expireApproval(ap?: PendingApproval): PendingApproval | undefined {
  if (!ap || ap.status !== 'pending') return ap
  return { ...ap, status: 'expired' }
}

function uid(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2)
}
