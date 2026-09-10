// Client for `xihe serve` (HTTP + WebSocket). The desktop owns no engine — it
// drives xihe over this neutral protocol. Server counterpart: gateway/serve/
// (one module per business: app/chat/admin/browser/terminal/knowledge).

/** Per-turn token usage carried on the `complete` event and historical
 *  assistant bubbles (`calls` = model invocations within the turn). */
export interface TurnUsage {
  prompt: number
  completion: number
  total: number
  calls?: number
}

export type ServeEvent =
  | { type: 'hello'; version: string; mode: string; model: string; capabilities: string[] }
  | { type: 'attached'; conv_id: string; running: boolean }
  /** 定时任务结果推送（DesktopChannel）：投递目标会话 + 文本（[定时任务: …] 前缀） */
  | { type: 'cron_result'; conv_id: string; text: string }
  | { type: 'turn_start'; turn_id: string; conv_id: string; session_key: string }
  | { type: 'text_delta'; turn_id: string; conv_id: string; text: string; by?: string }
  | { type: 'thought_delta'; turn_id: string; conv_id: string; text: string; by?: string }
  | { type: 'tool_call'; turn_id: string; conv_id: string; name: string; args: string; by?: string; id?: number }
  | { type: 'tool_result'; turn_id: string; conv_id: string; name: string; result: string; elapsed: number; truncated?: boolean; by?: string }
  | { type: 'approval_request'; turn_id: string; conv_id: string; id: string; name: string; summary: string; args?: string }
  | { type: 'approval_resolved'; turn_id: string; conv_id: string; id: string; approved: boolean; reason: string }
  | { type: 'complete'; turn_id: string; conv_id: string; text: string; reason?: string; session_id?: string; usage?: TurnUsage }
  | { type: 'error'; turn_id?: string; conv_id?: string; message: string }

export interface ServeHealth {
  ok: boolean
  version: string
  mode: string
  model: string
  capabilities: string[]
}

export interface ServeAgent {
  id: string
  name: string
  engine: string
  shape: string
  model: string
  status: string
  capabilities: string[]
  dataRoot?: string
  description?: string
}

// Default to the standard `xihe serve` port. Override with setServeBase()
// (e.g. from settings) if a different port/instance is used.
let baseUrl = 'http://127.0.0.1:7788'
let wsBase = 'ws://127.0.0.1:7788'
let wsUrl = wsBase + '/stream'

export function setServeBase(url: string): void {
  const u = url.replace(/\/$/, '')
  baseUrl = u
  wsBase = u.replace(/^http/, 'ws')
  wsUrl = wsBase + '/stream'
}

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${baseUrl}${path}`)
    if (!r.ok) return null
    return (await r.json()) as T
  } catch {
    return null // serve down / unreachable
  }
}

export function getHealth(): Promise<ServeHealth | null> {
  return getJson<ServeHealth>('/health')
}

/** POST /test-connection — server-side model-endpoint probe (serve GETs
 *  {base_url}/models with the stored key). The renderer never sees the key,
 *  so the test must run inside serve. Tests the SAVED config.yaml values;
 *  unsaved form edits don't participate. */
export interface TestConnectionResult {
  ok: boolean
  status_code: number | null
  models: string[]
  error: string | null
  model_configured?: string | null
}

export function testConnection(): Promise<TestConnectionResult | null> {
  return postJson<TestConnectionResult>('/test-connection')
}

export function getAgents(): Promise<ServeAgent[]> {
  return getJson<{ agents: ServeAgent[] }>('/agents').then((r) => r?.agents ?? [])
}

export interface HistoryMessage {
  role: string
  content: string
  /** Stable serve row id on the assistant bubble — the anchor used to
   *  lazy-fetch that turn's trace (set for every assistant turn, absent for
   *  user bubbles). */
  id?: number | null
  /** Tool-call count folded into this assistant turn (>0 → desktop shows a
   *  "N 个工具" trace header before the trace is even loaded). */
  tools?: number
  /** True when the turn had persisted reasoning — desktop shows a 思考 badge
   *  and mounts the trace before the trace is lazy-loaded. */
  has_reasoning?: boolean
  /** True when the turn never closed with a final reply (interrupted mid-loop
   *  or the serve process died mid-turn) — desktop shows a 未完成 badge
   *  instead of rendering the folded tool calls as a finished reply. */
  incomplete?: boolean
  /** ISO timestamp from the serve DB row — rendered as HH:mm on the bubble. */
  ts?: string
  /** Token usage persisted on the turn's final assistant row (cost badge). */
  usage?: TurnUsage
}

export function getHistory(convId: string): Promise<HistoryMessage[]> {
  return getJson<{ messages: HistoryMessage[] }>(
    `/convs/${encodeURIComponent(convId)}/messages`
  ).then((r) => r?.messages ?? [])
}

/** One event in a lazily-loaded historical trace (shape matches the live
 *  TraceEvent variants, minus runtime-only fields like elapsed/ts). A turn's
 *  reasoning is reconstructed as 'thought' items interleaved with 'tool' items
 *  in the order they occurred. */
export type HistoryTraceEvent =
  | {
      kind: 'tool'
      name: string
      args: string
      status: 'done'
      /** Tool output (server-truncated to ~4000 chars) when the trace was
       *  enriched with results; absent for older traces served before result
       *  enrichment. */
      result?: string
      truncated?: boolean
    }
  | { kind: 'thought'; text: string }

/** `GET /convs/{id}/trace/{msg_id}` — lazy-fetch a turn's tool calls by the
 *  anchor row id attached to its assistant bubble. Called on trace expand. */
export function getTrace(convId: string, msgId: number): Promise<HistoryTraceEvent[]> {
  return getJson<{ trace: HistoryTraceEvent[] }>(
    `/convs/${encodeURIComponent(convId)}/trace/${msgId}`
  ).then((r) => r?.trace ?? [])
}

/** `GET /toolresult?path=` — full content of a tool result spilled to the
 *  server's side-store (the trace/WS payload only carries the first ~15k
 *  chars). The path comes from the truncated text itself
 *  ("Full output saved to: <path>"). Returns null when serve rejects the
 *  read (path escaped the side-store dir / file gone). */
export function getFullToolResult(path: string): Promise<string | null> {
  return getJson<{ ok: boolean; content?: string }>(
    `/toolresult?path=${encodeURIComponent(path)}`
  ).then((r) => (r?.ok && typeof r.content === 'string' ? r.content : null))
}

/** `GET /toolargs?id=` — full args of a LIVE tool call (the tool_call event
 *  carries the first 500 chars + this id). Null = unknown/evicted (ring is
 *  bounded; history turns carry full args via the trace endpoint). */
export function getToolArgs(id: number): Promise<string | null> {
  return getJson<{ ok: boolean; args?: string }>(`/toolargs?id=${id}`).then(
    (r) => (r?.ok && typeof r.args === 'string' ? r.args : null)
  )
}

/** One conversation row from `GET /sessions` (serve platform, recent-first). */
export interface SessionRow {
  conv_id: string
  session_key: string
  title: string | null
  updated_at: string | null
  msg_count: number
}

export function listSessions(): Promise<SessionRow[]> {
  return getJson<{ sessions: SessionRow[] }>('/sessions').then((r) => r?.sessions ?? [])
}

// Read-only listings for the "管理" panel (GET /mcp /skills /cron). envelope
// keys + field names must match gateway/serve/admin.py exactly or getJson silently
// returns null → [].

/** One configured MCP server. `tools` is the count it registered; `connected`
 *  is false for configured-but-not-yet-connected servers. */
export interface McpServer {
  name: string
  transport: 'http' | 'stdio'
  tools: number
  connected: boolean
}

export function listMcp(): Promise<McpServer[]> {
  return getJson<{ servers: McpServer[] }>('/mcp').then((r) => r?.servers ?? [])
}

/** One discovered skill (bundled or user). `source` marks user-created (editable)
 *  vs bundled (read-only); the raw filesystem path is not exposed. */
export interface SkillInfo {
  name: string
  description: string
  category: string | null
  source: 'user' | 'bundled'
}

export function listSkills(): Promise<SkillInfo[]> {
  return getJson<{ skills: SkillInfo[] }>('/skills').then((r) => r?.skills ?? [])
}

/** One scheduled cron job (curated summary — full prompts/scripts are not sent). */
export interface CronJobInfo {
  job_id: string
  name: string
  schedule: string
  repeat: string
  enabled: boolean
  state?: string
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  last_error?: string | null
  deliver?: string
  origin?: { platform: string; chat_id: string } | null
  prompt?: string
  script?: string | null
  no_agent?: boolean
}

/** Scheduler liveness. */
export interface SchedulerHealth {
  running: boolean
  alive: boolean
  last_tick_ago: number | null
  agent_set: boolean
  adapter_set: boolean
}

export function listCron(): Promise<{ jobs: CronJobInfo[]; scheduler: SchedulerHealth | null }> {
  return getJson<{ jobs: CronJobInfo[]; scheduler: SchedulerHealth }>('/cron').then((r) =>
    r ? { jobs: r.jobs ?? [], scheduler: r.scheduler ?? null } : { jobs: [], scheduler: null }
  )
}

/** One persisted cron run (output/<job_id>/<timestamp>.md, newest first). */
export interface CronRunInfo {
  run_at: string
  content: string
}

export function listCronRuns(jobId: string, limit = 10): Promise<CronRunInfo[]> {
  return getJson<{ runs: CronRunInfo[] }>(`/cron/${encodeURIComponent(jobId)}/runs?limit=${limit}`).then(
    (r) => r?.runs ?? []
  )
}

export function deleteCron(jobId: string): Promise<{ ok: boolean; error?: string }> {
  return fetch(`${baseUrl}/cron/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
    .then((r) => r.json())
}

export function cronAction(
  jobId: string,
  action: 'run' | 'pause' | 'resume'
): Promise<{ ok: boolean; error?: string }> {
  return fetch(`${baseUrl}/cron/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' })
    .then((r) => r.json())
}

/** One specialist-agent spec — verbatim from its `<agent_home>/agents/<slug>.yaml`
 *  minus api_key (never echoed; only api_key_set crosses the wire). Fields may
 *  be absent; xihe applies its own defaults at load time. Empty model/base_url/
 *  api_key/max_iterations mean "inherit the main config". */
export interface SpecialistSpec {
  name?: string
  description?: string
  persona?: string
  toolsets?: string[]
  skills?: string[]
  model?: string
  base_url?: string
  api_key?: string
  max_iterations?: number
  project_context?: boolean
  enabled?: boolean
}

/** One row of GET /specialists — the raw spec plus the write-only-key flag.
 * `bundled`: the winning spec comes from src/agents (产品内置专家，不受
 * specialists.enabled 影响)；编辑会生成用户覆盖副本，删除副本即恢复内置。 */
export interface SpecialistEntry {
  slug: string
  spec: SpecialistSpec
  api_key_set: boolean
  bundled: boolean
}

/** One configured MCP server (for the specialist editor's per-server chips:
 *  selecting a server writes `mcp-<name>` into the spec's toolsets). */
export interface SpecialistMcpServer {
  name: string
  tools: number
  connected: boolean
}

/** One toolset catalog entry — `label` is the Chinese display name, `tools`
 *  the resolved tool count (0 for auto-populated groups with nothing
 *  registered yet). */
export interface SpecialistToolset {
  name: string
  label: string
  description: string
  tools: number
}

/** GET /specialists — every agents/*.yaml plus the toolset catalog (valid
 *  toolset names for the editor's chips), the MCP server list, and the
 *  dispatch tools the CURRENT process registered (synced live — every write
 *  and list read reconciles the registry with the yaml state). */
export interface SpecialistsInfo {
  specialists: SpecialistEntry[]
  /** config.yaml specialists.enabled — false = dispatch off, user
   *  run_*_agent not registered no matter how many files exist (distinct
   *  from 未注册, which means validation/collision dropped the entry). */
  specialists_enabled: boolean
  toolsets: SpecialistToolset[]
  mcp_servers: SpecialistMcpServer[]
  registered: string[]
}

export function listSpecialists(): Promise<SpecialistsInfo | null> {
  return getJson<SpecialistsInfo>('/specialists')
}

/** PUT /specialists/{slug} — write one specialist file (create or replace).
 *  api_key absent = keep the file's existing key; empty string = clear it. */
export function putSpecialist(
  slug: string,
  spec: SpecialistSpec
): Promise<{ ok: boolean; slug: string; warnings?: string[] } | null> {
  return fetch(`${baseUrl}/specialists/${encodeURIComponent(slug)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spec }),
  })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null)
}

/** DELETE /specialists/{slug} — remove one specialist file. */
export function deleteSpecialist(slug: string): Promise<boolean> {
  return fetch(`${baseUrl}/specialists/${encodeURIComponent(slug)}`, { method: 'DELETE' })
    .then((r) => r.ok)
    .catch(() => false)
}

async function postJson<T>(path: string, body?: unknown): Promise<T | null> {
  try {
    const r = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      ...(body !== undefined && {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    })
    if (!r.ok) return null
    return (await r.json()) as T
  } catch {
    return null // serve down / unreachable
  }
}

// ---- capability store (GET /store + POST /store/*). Envelope keys must
// match gateway/serve/admin.py exactly or getJson silently returns null.

/** One credential field an MCP catalog item needs at install time. */
export interface StoreConfigField {
  key: string
  label: string
  type?: string
  required?: boolean
}

/** One store catalog item joined with install state (GET /store). Secrets
 * never cross the wire — only which config keys are filled (`secrets_set`). */
export interface StoreItem {
  id: string
  type: 'skill' | 'mcp'
  title: string
  description?: string
  version?: string
  source_name?: string
  installed: boolean
  installed_version?: string | null
  upgradable?: boolean
  orphan?: boolean
  /** Read-only row for a manually-configured resource (config.yaml /
   *  skills dir) — no install/update/uninstall semantics. */
  manual?: boolean
  /** Catalog skill that's on disk outside the store ledger (hand-placed copy)
   *  — shows as installed, with a provenance badge. */
  hand_installed?: boolean
  unsupported?: string
  config_schema?: StoreConfigField[]
  secrets_set?: Record<string, boolean>
  /** Agent keys the item is mounted to ('main' or a specialist slug). */
  mounted?: string[]
}

export interface StoreSource {
  name: string
  ok: boolean
  error: string | null
}

export interface StoreView {
  items: StoreItem[]
  sources: StoreSource[]
}

/** GET /store — aggregated catalog + install state. */
export function listStore(): Promise<StoreView | null> {
  return getJson<StoreView>('/store')
}

/** POST /store/refresh — force re-fetch of every catalog source. */
export function refreshStore(): Promise<StoreView | null> {
  return postJson<StoreView>('/store/refresh')
}

/** POST /store/install — `config` carries MCP credential values; they are
 * only sent, never read back (the response carries no secret material). */
export function installStoreItem(
  type: 'skill' | 'mcp',
  id: string,
  config?: Record<string, string>
): Promise<{ ok: boolean; error?: string; mounted_hint?: string } | null> {
  return postStoreJson('/store/install', { type, id, config: config ?? {} })
}

export function uninstallStoreItem(
  type: 'skill' | 'mcp',
  id: string
): Promise<{ ok: boolean; error?: string } | null> {
  return postStoreJson('/store/uninstall', { type, id })
}

/** POST /store/mount — replace-semantics: `targets` is the full new mount
 * list ('main' = needs a serve restart; specialist slugs apply hot). */
export function mountStoreItem(
  type: 'skill' | 'mcp',
  id: string,
  targets: string[]
): Promise<
  | {
      ok: boolean
      error?: string
      mounted?: string[]
      effect?: Record<string, 'restart' | 'hot'>
    }
  | null
> {
  return postStoreJson('/store/mount', { type, id, targets })
}

async function postStoreJson<T>(path: string, body: unknown): Promise<T | null> {
  try {
    const r = await fetch(`${baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    // Error bodies ({ok:false,error}) arrive with 4xx status but still parse.
    return (await r.json()) as T
  } catch {
    return null // serve down / unreachable
  }
}

/** `POST /convs/{id}/reset` starts a fresh session round (same conv_id). */
export function resetSession(convId: string): Promise<boolean> {
  return postJson<{ reset: boolean }>(
    `/convs/${encodeURIComponent(convId)}/reset`
  ).then((r) => !!r?.reset)
}

// ---- memory + KBS (GET/PUT/DELETE /memory, GET /kbs, GET /kbs/page). Served
// by gateway/serve/knowledge.py — envelope keys must match exactly.

/** One long-term memory entry. Keys are a flat namespace; specialists
 *  conventionally prefix theirs `agent:<slug>:` (the browser groups on it). */
export interface MemoryEntry {
  key: string
  value: string
}

/** GET /memory — flat entries sorted by key. `corrupt` means memories.json
 *  failed to parse server-side; writes are refused (409) until forced. */
export interface MemoryView {
  items: MemoryEntry[]
  count: number
  corrupt: boolean
  error: string | null
}

export function listMemory(): Promise<MemoryView | null> {
  return getJson<MemoryView>('/memory')
}

/** PUT/DELETE /memory result — `error` carries the serve-side message on
 *  4xx (injection scan, corrupt store, rename race). */
export interface MemoryWriteResult {
  ok: boolean
  key?: string
  renamed_from?: string
  deleted?: boolean
  error?: string
}

/** PUT /memory — upsert one entry; `previousKey` renames in the same write.
 *  `overwriteCorrupt` discards a corrupt store (only after a user confirm). */
export function putMemory(
  key: string,
  value: string,
  previousKey?: string,
  overwriteCorrupt?: boolean
): Promise<MemoryWriteResult | null> {
  return sendJsonExpectError<MemoryWriteResult>('/memory', 'PUT', {
    key,
    value,
    previous_key: previousKey,
    overwrite_corrupt: overwriteCorrupt || undefined,
  })
}

/** DELETE /memory?key= — idempotent (`deleted` false when already gone).
 *  `force` proceeds on a corrupt store (after a user confirm). */
export function deleteMemory(key: string, force = false): Promise<MemoryWriteResult | null> {
  return sendJsonExpectError<MemoryWriteResult>(
    `/memory?key=${encodeURIComponent(key)}${force ? '&force=1' : ''}`,
    'DELETE'
  )
}

async function sendJsonExpectError<T>(
  path: string,
  method: string,
  body?: unknown
): Promise<T | null> {
  try {
    const r = await fetch(`${baseUrl}${path}`, {
      method,
      ...(body !== undefined && {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    })
    // Error bodies ({ok:false,error}) arrive with 4xx status but still parse.
    return (await r.json()) as T
  } catch {
    return null // serve down / unreachable
  }
}

/** One wiki page (entity/insight/concept) from GET /kbs — stem-scanned with
 *  index enrichment; `rel` is the root-relative posix path (may be Chinese). */
export interface KbsPageInfo {
  rel: string
  stem: string
  type: 'entity' | 'insight' | 'concept'
  domain: string | null
  title: string
  summary: string
  bytes: number
  mtime: string
}

/** Core wiki files (active/recent/index/log) and domain hubs. */
export interface KbsWikiFile {
  rel: string
  title: string
  kind: 'core' | 'hub'
}

/** One staging candidate (meta/candidates/*.md). */
export interface KbsCandidate {
  rel: string
  title: string
  status: 'open' | 'promoted' | 'merged' | 'dropped' | 'unknown'
  updated?: string | null
  domains?: string[]
  related_topic?: string
  next_action?: string
  why_it_matters?: string
  resolved_at?: string
  resolution_target?: string
}

export interface KbsDomain {
  slug: string
  aliases: string[]
}

/** Digest mirroring the kbs_status tool: lint health, active workstreams. */
export interface KbsStatusDigest {
  root?: string
  initialized?: boolean
  lint_status?: Record<string, unknown> & {
    lint_count?: number
    total_pages?: number
    last_lint?: string
  }
  last_lint_age_hours?: number | null
  lint_stale?: boolean
  active_workstreams?: string[] | null
  recent_update_rows?: number | null
  hints?: string[]
  message?: string
}

/** GET /kbs — one aggregate for the whole browser view. `enabled` false =
 *  config kbs.enabled off; `initialized` false = root missing. */
export interface KbsView {
  enabled: boolean
  initialized: boolean
  root: string
  status: KbsStatusDigest
  domains: KbsDomain[]
  pages: KbsPageInfo[]
  wiki: KbsWikiFile[]
  candidates: KbsCandidate[]
}

export function listKbs(): Promise<KbsView | null> {
  return getJson<KbsView>('/kbs')
}

/** GET /kbs/page?path= — one markdown page, full content (never truncated);
 *  `ok` false with `error` (path-rejected / not-found) is a normal body, not
 *  a status code. */
export interface KbsPage {
  ok: boolean
  rel?: string
  title?: string
  content?: string
  bytes?: number
  error?: string
}

export function getKbsPage(rel: string): Promise<KbsPage | null> {
  return getJson<KbsPage>(`/kbs/page?path=${encodeURIComponent(rel)}`)
}

/** `POST /convs/{id}/truncate` rolls a conversation back: the user row
 * `fromMsgId` and everything after it is deleted (desktop 重新发送). */
export function truncateConversation(convId: string, fromMsgId: number): Promise<boolean> {
  return postJson<{ ok: boolean }>(
    `/convs/${encodeURIComponent(convId)}/truncate`,
    { from_msg_id: fromMsgId }
  ).then((r) => !!r?.ok)
}

// ---- browser panel (GET /browser/status + POST /browser/*). The Chrome
// window itself is snapped by main via serve; these are the panel's
// lifecycle queries (poll/launch/un-hide).

/** GET /browser/status — agent CDP Chrome process/window state. `rect` is
 *  [l,t,r,b] physical px; `snapped` the last snap rect [x,y,w,h]. */
export interface BrowserStatus {
  ok: boolean
  supported: boolean
  port?: number
  running?: boolean
  pid?: number | null
  hwnd?: number | null
  snapped?: number[] | null
  hidden?: boolean
  rect?: number[] | null
}

export function getBrowserStatus(): Promise<BrowserStatus | null> {
  return getJson<BrowserStatus>('/browser/status')
}

/** POST /browser/launch — cold-start the CDP Chrome (up to ~8s; the response
 *  already carries the fresh status). */
export function launchBrowser(): Promise<BrowserStatus | null> {
  return postJson<BrowserStatus>('/browser/launch')
}

/** POST /browser/show — re-show the hidden snapped Chrome at its cached rect
 *  (placeholder-click recovery; empty body is the contract). */
export function showBrowser(): Promise<BrowserStatus | null> {
  return postJson<BrowserStatus>('/browser/show')
}

/** POST /browser/hide — hide the snapped Chrome (tab switched away from the
 *  chat layout; serve's foreground guard no-ops if the user is IN Chrome). */
export function hideBrowser(): Promise<BrowserStatus | null> {
  return postJson<BrowserStatus>('/browser/hide')
}

/** POST /browser/restart — kill + relaunch the CDP Chrome so a pushed
 *  appearance (theme) applies to the new launch. Blocking (taskkill + port
 *  poll + relaunch, up to ~8s); response carries `restarted` + fresh status.
 *  Login state survives (cdp-profile); open tabs do not. */
export function restartBrowser(): Promise<(BrowserStatus & { restarted?: boolean }) | null> {
  return postJson<BrowserStatus & { restarted?: boolean }>('/browser/restart')
}

// ---- ssh live terminal (GET /ssh/live, WS /ssh/live/stream, POST
// /ssh/live/connect). Served by gateway/serve/terminal.py — these
// back the terminal panel: sessions live in the SERVE process (shared with
// the agent's ssh tools), the panel is just a viewer + second keyboard.

/** One watched ssh session from GET /ssh/live. `key` is the registry key
 *  (`session_key|alias` — the stream route's address); `name` is the display
 *  alias. `offset` is the ring's write cursor (chars ever); `buffered` how
 *  much scrollback is still retained. `origin` is who connected (agent via
 *  ssh_connect vs desktop quick-connect); `session_key` groups
 *  agent-triggered sessions to the conversation that ran them ("" for
 *  desktop-initiated or pre-existing connections). */
export interface SshLiveSession {
  key: string
  name: string
  host: string
  user: string
  mode: string
  origin: 'agent' | 'desktop' | string
  session_key: string
  cols: number | null
  rows: number | null
  alive: boolean
  offset: number
  buffered: number
  last_input: string | null
  last_activity: number | null
  closed: boolean
}

/** Null = serve unreachable (fetch failed) — callers keep retrying instead of
 *  mistaking an outage for "sessions gone". */
export function getSshLive(): Promise<SshLiveSession[] | null> {
  return getJson<{ sessions: SshLiveSession[] }>('/ssh/live').then(
    (r) => (r ? r.sessions ?? [] : null)
  )
}

/** POST /ssh/live/connect — desktop-initiated connect through the same
 *  _ssh_connect the agent uses (the session lands in the shared registry).
 *  The token is sent once and never echoed back; on ok the panel attaches to
 *  the session by name. `sessionKey` (the current conversation's key) only
 *  affects grouping in the panel. */
export interface SshConnectResult {
  ok: boolean
  success?: boolean
  session?: string
  host?: string
  mode?: string
  message?: string
  error?: string
}

export function sshConnect(body: {
  name: string
  host?: string
  port?: number
  user?: string
  token: string
  mode?: string
  sessionKey?: string
}): Promise<SshConnectResult | null> {
  return postStoreJson<SshConnectResult>('/ssh/live/connect', {
    name: body.name,
    host: body.host || '',
    port: body.port || 22,
    user: body.user || '',
    token: body.token,
    mode: body.mode || 'shell',
    session_key: body.sessionKey || '',
  })
}

/** Server→client frames on /ssh/live/stream. `d.o` is the char offset AFTER
 *  the frame's text — the resume cursor for reconnects. `i` attributes a
 *  write (agent vs user) on top of the pty echo that's already in the data.
 *  `resize` is the pty size change (echoed from THIS viewer's fit, or pushed
 *  when ANOTHER viewer resized the shared pty — adopt it either way). */
export type SshStreamFrame =
  | {
      t: 'meta'
      key: string
      name: string
      host?: string
      user?: string
      mode?: string
      origin?: string
      session_key?: string
      cols?: number | null
      rows?: number | null
      offset: number
      base: number
    }
  | { t: 'd'; o: number; s: string }
  | { t: 'i'; who: string; s: string }
  | { t: 'resize'; cols: number; rows: number }
  | { t: 'x'; reason: string }
  | { t: 'err'; reason: string }

export interface SshStream {
  /** Send keystrokes/lines to the remote channel (term.onData wiring). */
  send: (text: string) => void
  /** Resize the shared pty (panel geometry changed → remote follows). */
  resize: (cols: number, rows: number) => void
  close: () => void
}

/** Open the terminal stream for one session. `from` resumes at a prior
 *  cursor; omit for first attach (serve sends the last ~8k of scrollback).
 * Resolves once the WS is open; rejects when the upgrade fails (serve down,
 * or the session no longer exists — the browser WS API hides the status). */
export function connectSshStream(
  key: string,
  from: number | undefined,
  onFrame: (f: SshStreamFrame) => void,
  onStatus: (connected: boolean) => void
): Promise<SshStream> {
  const q = from !== undefined ? `&from=${from}` : ''
  return new Promise((resolve, reject) => {
    let ws: WebSocket
    try {
      ws = new WebSocket(`${wsBase}/ssh/live/stream?key=${encodeURIComponent(key)}${q}`)
    } catch (e) {
      reject(e)
      return
    }
    let settled = false
    ws.onopen = () => {
      if (settled) return
      settled = true
      onStatus(true)
      resolve({
        send: (text) => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'input', s: text }))
        },
        resize: (cols, rows) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ t: 'resize', cols, rows }))
        },
        close: () => {
          try {
            ws.close()
          } catch {
            /* noop */
          }
        },
      })
    }
    ws.onmessage = (ev) => {
      try {
        onFrame(JSON.parse(ev.data) as SshStreamFrame)
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onclose = () => {
      onStatus(false)
      if (!settled) {
        settled = true
        reject(new Error('ssh stream closed before open'))
      }
    }
    ws.onerror = () => {
      if (!settled) {
        settled = true
        reject(new Error('ssh stream connect failed'))
      }
    }
  })
}

// ---- agent local channels (GET /local/live, WS /local/live/stream, POST
// /local/live/stop). Backs the terminal panel's agent tabs: every
// conversation's `terminal` console (kind 'conv') and every resident process
// (kind 'proc') streams into serve's channel registry; this is a read-only
// viewer (no input path) plus a stop button for live processes.

/** One channel row from GET /local/live. `offset`/`buffered` are the ring
 *  cursors (attach/resume addressing); proc rows also carry pid/start/exit. */
export interface LocalChannel {
  key: string
  kind: 'conv' | 'proc'
  name: string
  session_key: string
  running: boolean
  command: string | null
  cwd: string | null
  offset: number
  buffered: number
  pid?: number | null
  started_at?: number | null
  exit_code?: number | null
}

/** Null = serve unreachable — same contract as getSshLive. */
export function getLocalChannels(): Promise<LocalChannel[] | null> {
  return getJson<{ channels: LocalChannel[] }>('/local/live').then((r) =>
    r ? r.channels ?? [] : null
  )
}

/** Server→client frames on /local/live/stream. `d.o` is the char offset AFTER
 *  the frame's text — the resume cursor for reconnects (same contract as the
 *  ssh stream). */
export type LocalStreamFrame =
  | {
      t: 'meta'
      key: string
      kind: 'conv' | 'proc'
      name: string
      session_key: string
      offset: number
      base: number
      running: boolean
      command: string | null
      cwd: string | null
    }
  | { t: 'd'; o: number; s: string }

export interface LocalStream {
  close: () => void
}

/** Open one agent local channel's stream. `from` resumes at a prior cursor;
 * omit for first attach (serve sends the last ~8k of scrollback). Resolves
 * once the WS is open; rejects when the upgrade fails (serve down, or the
 * channel is gone — only proc channels vanish; conv keys are created on
 * demand). */
export function connectLocalStream(
  key: string,
  from: number | undefined,
  onFrame: (f: LocalStreamFrame) => void,
  onStatus: (connected: boolean) => void
): Promise<LocalStream> {
  const q =
    (key ? `key=${encodeURIComponent(key)}` : '') + (from !== undefined ? `&from=${from}` : '')
  return new Promise((resolve, reject) => {
    let ws: WebSocket
    try {
      ws = new WebSocket(`${wsBase}/local/live/stream?${q}`)
    } catch (e) {
      reject(e)
      return
    }
    let settled = false
    ws.onopen = () => {
      if (settled) return
      settled = true
      onStatus(true)
      resolve({
        close: () => {
          try {
            ws.close()
          } catch {
            /* noop */
          }
        },
      })
    }
    ws.onmessage = (ev) => {
      try {
        onFrame(JSON.parse(ev.data) as LocalStreamFrame)
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onclose = () => {
      onStatus(false)
      if (!settled) {
        settled = true
        reject(new Error('local stream closed before open'))
      }
    }
    ws.onerror = () => {
      if (!settled) {
        settled = true
        reject(new Error('local stream connect failed'))
      }
    }
  })
}

/** POST /local/live/stop — kill a channel's live resident process (the panel
 *  stop button). 404 (unknown/dead channel) resolves to ok:false, not null —
 *  it's a benign race when the process just exited on its own. */
export function stopLocalChannel(key: string): Promise<{ ok: boolean } | null> {
  return postStoreJson<{ ok: boolean }>('/local/live/stop', { key })
}

/** `DELETE /convs/{id}` deletes a conversation + its transcript server-side. */
export function deleteSession(convId: string): Promise<boolean> {
  return fetch(`${baseUrl}/convs/${encodeURIComponent(convId)}`, { method: 'DELETE' })
    .then((r) => (r.ok ? r.json() : null))
    .then((r: { deleted?: boolean } | null) => !!r?.deleted)
    .catch(() => false) // serve down / unreachable
}

/** `POST /convs/{id}/title` renames a conversation server-side. */
export function setConversationTitle(convId: string, title: string): Promise<boolean> {
  return fetch(`${baseUrl}/convs/${encodeURIComponent(convId)}/title`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
    .then((r) => (r.ok ? r.json() : null))
    .then((r: { ok?: boolean } | null) => !!r?.ok)
    .catch(() => false) // serve down / unreachable
}

export interface ServeStream {
  /** `cwd` is the workspace workdir for this turn — when set, serve threads it
   *  into the agent so relative paths / terminal resolve there. Omit (undefined)
   *  for unbound conversations; the frame then carries no cwd and serve falls
   *  back to its process cwd. */
  sendTurn: (convId: string, text: string, cwd?: string) => void
  /** Re-attach this (new) socket to a conv whose turn is still running
   *  server-side after a reconnect — its deltas/complete resume flowing here.
   *  serve acks with an `attached` event carrying `running`; false means no
   *  live turn (nothing will stream). */
  attach: (convId: string) => void
  interrupt: (convId: string) => void
  /** Non-interrupting redirect of the running turn; no-op if idle. */
  steer: (convId: string, text: string) => void
  /** Answer the active turn's pending approval (card buttons). always pairs
   *  with approval: remember this exact call for the rest of the session. */
  approve: (convId: string, id: string, approved: boolean, always?: boolean) => void
  close: () => void
}

/**
 * Open the streaming WebSocket. Resolves with a control handle once connected;
 * rejects if the connection fails before opening. `onStatus(false)` fires on
 * any later drop so the caller can fall back / reconnect.
 */
export function connectStream(
  onEvent: (e: ServeEvent) => void,
  onStatus: (connected: boolean) => void
): Promise<ServeStream> {
  return new Promise((resolve, reject) => {
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl)
    } catch (e) {
      reject(e)
      return
    }
    let settled = false
    ws.onopen = () => {
      if (settled) return
      settled = true
      onStatus(true)
      resolve({
        sendTurn: (convId, text, cwd) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(
              JSON.stringify(
                cwd
                  ? { type: 'send', conv_id: convId, text, cwd }
                  : { type: 'send', conv_id: convId, text }
              )
            )
        },
        interrupt: (convId) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ type: 'interrupt', conv_id: convId }))
        },
        attach: (convId) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ type: 'attach', conv_id: convId }))
        },
        steer: (convId, text) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ type: 'steer', conv_id: convId, text }))
        },
        approve: (convId, id, approved, always) => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({
              type: 'approve', conv_id: convId, id, approved,
              always: always || undefined,
            }))
        },
        close: () => {
          try {
            ws.close()
          } catch {
            /* noop */
          }
        },
      })
    }
    ws.onmessage = (ev) => {
      try {
        onEvent(JSON.parse(ev.data) as ServeEvent)
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onclose = () => {
      onStatus(false)
      if (!settled) {
        settled = true
        reject(new Error('ws closed before open'))
      }
    }
    ws.onerror = () => {
      if (!settled) {
        settled = true
        reject(new Error('ws connect failed'))
      }
    }
  })
}
