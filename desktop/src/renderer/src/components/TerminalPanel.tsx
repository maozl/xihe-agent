import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { Plus, RefreshCw, Square } from 'lucide-react'
import {
  connectLocalStream,
  connectSshStream,
  getLocalChannels,
  getSshLive,
  sshConnect,
  stopLocalChannel,
  type LocalChannel,
  type LocalStream,
  type SshLiveSession,
  type SshStream,
} from '../lib/serveClient'
import { desktop } from '../lib/desktop'
import { useStore } from '../store'
import { cn } from '../lib/cn'
import { Resizer, usePanelSize } from './Resizer'

/** Sentinel tab key for the LOCAL shell (owned by main's ConPTY, not serve's
 *  registries). Every other tab is a serve key: a channel key (`conv:` /
 *  `proc:`) or an ssh session key (`session_key|alias`). */
const LOCAL = '__local__'

const POLL_MS = 3_000
const POLL_DEAD_MS = 1_000
const RETRY_MS = 1_500
// Scrollback chars replayed when (re)attaching — mirrors serve's own first
// attach backlog; a tab switched back re-sees the tail instead of an
// empty screen with a live cursor.
const REPLAY_CHARS = 8_192

const MIN_HEIGHT = 200
const DEFAULT_HEIGHT = 320
const HEIGHT_KEY = 'terminalPanelHeight'

const isLocalKey = (k: string): boolean => k.startsWith('conv:') || k.startsWith('proc:')

/** Short per-conversation tag from a session_key (…:dm:{chat_id}) — the tab
 *  badge for OTHER conversations' channels. */
const convTail = (sk: string): string => {
  const segs = sk.split(':').filter(Boolean)
  const tail = segs[segs.length - 1] ?? sk
  return tail.length > 10 ? tail.slice(0, 10) + '…' : tail
}

// The pty starts at 100×30 (server default) and follows the viewer's fitted
// size via {t:"resize"} — wrapping is computed at the panel's real width.
// Palette stays dark in both app themes (remote 256-color output is tuned
// for a dark background).
const FIT_DEBOUNCE_MS = 150

export function TerminalPanel({ className }: { className?: string }) {
  const [sessions, setSessions] = useState<SshLiveSession[]>([])
  const [channels, setChannels] = useState<LocalChannel[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  // Bumped to force a terminal/stream rebuild for the SAME tab key — the
  // agent reconnects under the same alias/channel and the panel must revive.
  const [attachEpoch, setAttachEpoch] = useState(0)
  const [deadReason, setDeadReason] = useState<string | null>(null)
  // Mirror for the poll loop (its effect deps don't include deadReason).
  const deadRef = useRef<string | null>(null)
  const [streamOk, setStreamOk] = useState(false)
  const [badge, setBadge] = useState<{ who: string; at: number } | null>(null)
  const [height, setHeight] = usePanelSize(HEIGHT_KEY, DEFAULT_HEIGHT, MIN_HEIGHT)
  const [connectOpen, setConnectOpen] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [connectErr, setConnectErr] = useState<string | null>(null)
  const [form, setForm] = useState({
    name: '',
    host: '',
    port: '22',
    user: '',
    token: '',
    mode: 'shell',
  })

  const hostRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const streamRef = useRef<SshStream | LocalStream | null>(null)
  // tab key → resume cursor (char offset after the last frame we rendered)
  const cursorsRef = useRef<Record<string, number>>({})
  // Proc channel keys ever listed — identifies the NEW process channel a
  // pending agentTermTarget refers to (targets resolve only to fresh keys).
  const seenProcsRef = useRef<Set<string>>(new Set())
  const selectedRef = useRef(selected)
  selectedRef.current = selected

  const convSessionKeys = useStore((s) => s.convSessionKeys)
  const activeConvId = useStore(
    (s) => s.agents.find((a) => a.id === s.selectedAgentId)?.activeConvId
  )
  const activeSessionKey = activeConvId ? convSessionKeys[activeConvId] : undefined
  // Local shell's seed cwd = the active conversation's workspace (only used
  // when main spawns it; afterwards the shell owns its own cwd).
  const convWorkspace = useStore((s) => s.convWorkspace)
  const workspaces = useStore((s) => s.workspaces)
  const wsId = activeConvId ? convWorkspace[activeConvId] : undefined
  const localCwd = wsId ? workspaces.find((w) => w.id === wsId)?.workdir : undefined

  // Session/channel list poll. Keeps a vanished selected tab selected — the
  // stream's dead state is the truth, not the list.
  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    const tick = async () => {
      const [rows, chans] = await Promise.all([getSshLive(), getLocalChannels()])
      if (!alive) return
      if (rows) setSessions(rows)
      if (chans) setChannels(chans)
      // Local tab is the fallback default — the terminal always has something
      // to show even with no channels/sessions.
      setSelected((cur) => cur ?? LOCAL)
      // Revive: a dead selected tab whose key reappears means a fresh ring
      // under the same key (agent reconnected the alias / restarted the
      // process) — the old cursor is meaningless. Drop it and rebuild
      // (attachEpoch re-runs the terminal effect though `selected` never
      // changed).
      const sel = selectedRef.current
      if (sel && deadRef.current) {
        const revived =
          rows?.some((r) => r.key === sel && r.alive && !r.closed) ??
          chans?.some((c) => c.key === sel)
        if (revived) {
          deadRef.current = null
          setDeadReason(null)
          delete cursorsRef.current[sel]
          setAttachEpoch((e) => e + 1)
        }
      }
      // Poll faster while the selected tab is dead — the interesting event
      // (the key coming back) should revive the terminal within a second,
      // not a full POLL_MS cycle.
      timer = setTimeout(() => void tick(), deadRef.current ? POLL_DEAD_MS : POLL_MS)
    }
    void tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [refreshKey])

  // The Agent console tab follows the active conversation: switching
  // conversations moves a conv-channel selection onto the new conversation's
  // console. LOCAL / ssh / proc tabs stay pinned — the user may be typing
  // there or watching a build; their badges mark what belongs to this conv.
  useEffect(() => {
    if (!activeSessionKey) return
    setSelected((cur) => (cur?.startsWith('conv:') ? `conv:${activeSessionKey}` : cur))
  }, [activeSessionKey])

  // An agent tool run in the active conversation wants this panel focused:
  // a conv target selects its channel immediately (created on demand
  // server-side); a proc target waits for the FIRST NEW proc channel under
  // that session to appear in a list refresh. Keys are marked seen AFTER
  // resolving — effects run in order, so marking first would hide the new
  // channel from the very target that caused it.
  const agentTermTarget = useStore((s) => s.agentTermTarget)
  const clearAgentTermTarget = useStore((s) => s.clearAgentTermTarget)
  useEffect(() => {
    if (agentTermTarget?.kind === 'conv') {
      setSelected(agentTermTarget.key)
      clearAgentTermTarget()
    } else if (agentTermTarget?.kind === 'proc') {
      // Newest unseen wins (reverse of insertion order): when two starts
      // land in the same poll burst, the pending target came from the LATER
      // tool call.
      let fresh: LocalChannel | undefined
      for (let i = channels.length - 1; i >= 0; i--) {
        const c = channels[i]
        if (
          c.kind === 'proc' &&
          c.session_key === agentTermTarget.sessionKey &&
          !seenProcsRef.current.has(c.key)
        ) {
          fresh = c
          break
        }
      }
      if (fresh) {
        setSelected(fresh.key)
        clearAgentTermTarget()
      }
    }
    for (const c of channels) {
      if (c.kind === 'proc') seenProcsRef.current.add(c.key)
    }
  }, [agentTermTarget, channels, clearAgentTermTarget])

  // Terminal + stream for the selected tab. Rebuilt on every switch —
  // xterm instances aren't cheap to keep hidden, and the replay tail makes
  // the fresh view feel continuous.
  useEffect(() => {
    if (!selected) return
    const host = hostRef.current
    if (!host) return

    let alive = true
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    setDeadReason(null)
    deadRef.current = null
    setStreamOk(false)
    const term = new Terminal({
      cols: 100,
      rows: 30,
      fontFamily: 'Consolas, "Courier New", monospace',
      fontSize: 12,
      cursorBlink: true,
      scrollback: 5000,
      theme: {
        background: '#0a0a0a',
        foreground: '#d4d4d4',
        cursor: '#d4d4d4',
        selectionBackground: '#64748b',
      },
    })
    termRef.current = term
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host)

    // Fit the terminal to the panel and push the size to the pty — the pty
    // follows the VIEWER (resize_pty on the server), so wrapping is computed
    // at the real width and the panel is fully covered. Debounced: the panel
    // drag fires the observer per frame.
    const applyFit = (): void => {
      try {
        const dims = fit.proposeDimensions()
        if (!dims || !dims.cols || !dims.rows) return
        if (dims.cols === term.cols && dims.rows === term.rows) return
        term.resize(dims.cols, dims.rows)
        if (closed) return
        if (selected === LOCAL) void desktop.localPtyResize(dims.cols, dims.rows)
        else {
          const s = streamRef.current
          if (s && 'resize' in s) s.resize(dims.cols, dims.rows)
        }
      } catch {
        /* disposed mid-fit */
      }
    }
    let fitTimer: ReturnType<typeof setTimeout> | null = null
    const ro = new ResizeObserver(() => {
      if (fitTimer) clearTimeout(fitTimer)
      fitTimer = setTimeout(applyFit, FIT_DEBOUNCE_MS)
    })
    ro.observe(host)

    // Local pty tab: the shell lives in main (survives tab switches / panel
    // close); this panel just attaches, replays the tail backlog, streams.
    let localOff: (() => void) | null = null
    let dataSub: { dispose(): void } | null = null

    if (selected === LOCAL) {
      void desktop
        .localPtyAttach(localCwd, term.cols, term.rows)
        .then((r) => {
          if (!alive) return
          if (!r.ok) {
            closed = true
            setDeadReason(r.reason)
            deadRef.current = r.reason
            return
          }
          term.write(r.backlog)
          applyFit()
          term.focus()
        })
      localOff = desktop.onLocalPtyEvent((ev) => {
        if (!alive || closed) return
        if (ev.t === 'out') {
          term.write(ev.chunk)
          setStreamOk(true)
        } else {
          closed = true
          setDeadReason(ev.reason)
          deadRef.current = ev.reason
          term.write(`\r\n\x1b[90m—— 本地终端已退出（${ev.reason}）——\x1b[0m\r\n`)
        }
      })
      dataSub = term.onData((d) => {
        if (!closed) void desktop.localPtyInput(d)
      })
    } else if (isLocalKey(selected)) {
      // Agent channels (conv console / resident process): read-only
      // cursor-addressed stream off serve's channel ring (same resume
      // contract as ssh). No onData — the viewer cannot type into the
      // agent's process.
      const attach = (): void => {
        if (!alive || closed) return
        // First-ever attach: no cursor → serve's default tail. Any re-attach
        // (WS drop mid-stream, tab switched back): resume at the cursor minus
        // a replay tail — serve clamps to its ring base.
        const cursor = cursorsRef.current[selected]
        const from = cursor !== undefined ? Math.max(0, cursor - REPLAY_CHARS) : undefined
        let wasOpen = false
        connectLocalStream(
          selected,
          from,
          (f) => {
            if (!alive) return
            if (f.t === 'd') {
              term.write(f.s)
              cursorsRef.current[selected] = f.o
            }
          },
          (ok) => {
            if (!alive) return
            setStreamOk(ok)
            // Serve restart / socket hiccup after the stream was live —
            // resume at the cursor; the channel ring keeps the history.
            if (ok) wasOpen = true
            else if (wasOpen && !closed) retry = setTimeout(attach, RETRY_MS)
          }
        )
          .then((s) => {
            if (!alive) {
              s.close()
              return
            }
            streamRef.current = s
            applyFit()
          })
          .catch(() => {
            if (!alive || closed) return
            // Upgrade failed: serve down, or an evicted proc channel.
            // Distinguish via the list (null = serve unreachable → retry) —
            // a gone channel must not retry forever.
            void getLocalChannels().then((rows) => {
              if (!alive || closed) return
              if (rows === null) {
                retry = setTimeout(attach, RETRY_MS)
                return
              }
              if (!rows.some((c) => c.key === selected)) {
                closed = true
                setDeadReason('channel-gone')
                deadRef.current = 'channel-gone'
                return
              }
              retry = setTimeout(attach, RETRY_MS)
            })
          })
      }
      attach()
    } else {
      const attach = (): void => {
        if (!alive || closed) return
        // First-ever attach: no cursor → serve's default tail. Any re-attach
        // (WS drop mid-stream, tab switched back): resume at the cursor minus
        // a replay tail — serve clamps to its ring base.
        const cursor = cursorsRef.current[selected]
        const from = cursor !== undefined ? Math.max(0, cursor - REPLAY_CHARS) : undefined
        let wasOpen = false
        connectSshStream(
          selected,
          from,
          (f) => {
            if (!alive) return
            if (f.t === 'meta') {
              // Adopt the pty's current size (initial 100×30 or another
              // viewer's); applyFit immediately re-fits to THIS panel.
              try {
                term.resize(f.cols || 100, f.rows || 30)
              } catch {
                /* keep default size */
              }
            } else if (f.t === 'd') {
              term.write(f.s)
              cursorsRef.current[selected] = f.o
            } else if (f.t === 'i') {
              setBadge({ who: f.who, at: Date.now() })
            } else if (f.t === 'resize') {
              // Another viewer resized the shared pty (or our own echo, a
              // same-size no-op) — follow it.
              try {
                term.resize(f.cols, f.rows)
              } catch {
                /* disposed */
              }
            } else if (f.t === 'x') {
              closed = true
              setDeadReason(f.reason)
              deadRef.current = f.reason
              // The ring died with the tap — a later re-attach (revive path or
              // manual) must treat it as first attach, not resume old offsets.
              delete cursorsRef.current[selected]
              term.write(`\r\n\x1b[90m—— 会话已断开（${f.reason}）——\x1b[0m\r\n`)
            } else if (f.t === 'err') {
              term.write(`\r\n\x1b[90m[输入未送达：${f.reason}]\x1b[0m\r\n`)
            }
          },
          (ok) => {
            if (!alive) return
            setStreamOk(ok)
            // A drop AFTER the stream was live (serve restart, socket hiccup)
            // resumes at the cursor; a pre-open failure is handled by the
            // catch below (it may be a vanished session, not worth retrying).
            if (ok) wasOpen = true
            else if (wasOpen && !closed) retry = setTimeout(attach, RETRY_MS)
          }
        )
          .then((s) => {
            if (!alive) {
              s.close()
              return
            }
            streamRef.current = s
            applyFit()
            term.focus()
          })
          .catch(() => {
            if (!alive || closed) return
            // Upgrade failed: serve down, or the session is gone (closed taps
            // unregister server-side). Distinguish via the list (null = serve
            // unreachable → retry) — a gone session must not retry forever.
            void getSshLive().then((rows) => {
              if (!alive || closed) return
              if (rows === null) {
                retry = setTimeout(attach, RETRY_MS)
                return
              }
              if (!rows.some((r) => r.key === selected)) {
                closed = true
                setDeadReason('session-gone')
                deadRef.current = 'session-gone'
                return
              }
              retry = setTimeout(attach, RETRY_MS)
            })
          })
      }
      attach()

      // onData carries keystrokes AND control sequences (arrows, ^C) — exactly
      // what a terminal should forward. Desktop typing is the user's own hand;
      // it does not go through the agent approvals pipeline.
      dataSub = term.onData((d) => {
        if (closed) return
        const s = streamRef.current
        if (s && 'send' in s) s.send(d)
      })
    }

    return () => {
      alive = false
      if (retry) clearTimeout(retry)
      if (fitTimer) clearTimeout(fitTimer)
      dataSub?.dispose()
      localOff?.()
      // Detach (not kill): the local shell keeps running in main so its state
      // survives switching to another tab and back.
      if (selected === LOCAL) void desktop.localPtyDetach()
      ro.disconnect()
      streamRef.current?.close()
      streamRef.current = null
      term.dispose()
      termRef.current = null
    }
  }, [selected, attachEpoch])

  // Fade the agent/user attribution badge.
  useEffect(() => {
    if (!badge) return
    const t = setTimeout(() => setBadge(null), 4_000)
    return () => clearTimeout(t)
  }, [badge])

  // Dead local shell → fresh one: kill (main nulls the session) and re-run the
  // attach effect via its epoch.
  const restartLocal = (): void => {
    void desktop.localPtyKill()
    deadRef.current = null
    setDeadReason(null)
    setAttachEpoch((e) => e + 1)
  }

  const onConnect = async (): Promise<void> => {
    if (!form.name.trim() || !form.token.trim()) {
      setConnectErr('name 和 Token/密码 必填')
      return
    }
    setConnecting(true)
    setConnectErr(null)
    const r = await sshConnect({
      name: form.name.trim(),
      host: form.host.trim() || undefined,
      port: Number(form.port) || 22,
      user: form.user.trim() || undefined,
      token: form.token,
      mode: form.mode,
      sessionKey: activeSessionKey,
    })
    setConnecting(false)
    if (r?.ok) {
      setConnectOpen(false)
      setForm((f) => ({ ...f, token: '' }))
      setSelected(form.name.trim())
      setRefreshKey((k) => k + 1)
    } else {
      setConnectErr(r?.error || r?.message || '连接失败')
    }
  }

  const convChannels = channels.filter((c) => c.kind === 'conv')
  const procChannels = channels.filter((c) => c.kind === 'proc')
  const selChannel = channels.find((c) => c.key === selected) ?? null
  const selProcRunning = selChannel?.kind === 'proc' && selChannel.running

  const stopProc = (): void => {
    if (!selChannel) return
    void stopLocalChannel(selChannel.key).then(() => setRefreshKey((k) => k + 1))
  }

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

      <header className="flex items-center gap-1 border-b border-line px-3 py-1.5">
        <div className="flex min-w-0 items-center gap-1 overflow-x-auto">
          <button
            onClick={() => {
              if (selected === LOCAL && deadReason) restartLocal()
              else setSelected(LOCAL)
            }}
            title="本地终端（本机 shell，与 SSH 无关）"
            className={cn(
              'flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs transition',
              selected === LOCAL
                ? 'bg-contrast text-ink'
                : 'text-ink-3 hover:bg-elevated hover:text-ink'
            )}
          >
            <span
              className={cn(
                'h-1.5 w-1.5 rounded-full',
                selected === LOCAL && deadReason ? 'bg-danger' : 'bg-accent'
              )}
            />
            <span>本地</span>
          </button>
          {convChannels.map((c) => (
            <button
              key={c.key}
              onClick={() => setSelected(c.key)}
              title={
                c.command
                  ? `${c.session_key} · 最近命令：${c.command}`
                  : `agent 本地命令控制台（${c.session_key}）`
              }
              className={cn(
                'flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs transition',
                selected === c.key
                  ? 'bg-contrast text-ink'
                  : 'text-ink-3 hover:bg-elevated hover:text-ink'
              )}
            >
              <span
                className={cn(
                  'h-1.5 w-1.5 rounded-full',
                  c.running ? 'animate-pulse bg-success' : 'bg-ink-4'
                )}
              />
              <span>
                {c.session_key === activeSessionKey
                  ? 'Agent'
                  : `Agent·${convTail(c.session_key)}`}
              </span>
            </button>
          ))}
          {procChannels.map((c) => (
            <button
              key={c.key}
              onClick={() => setSelected(c.key)}
              title={`${c.command ?? c.name} · ${c.cwd ?? ''} · ${
                c.session_key === activeSessionKey ? '本对话' : c.session_key
              }`}
              className={cn(
                'flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs transition',
                selected === c.key
                  ? 'bg-contrast text-ink'
                  : 'text-ink-3 hover:bg-elevated hover:text-ink'
              )}
            >
              <span
                className={cn(
                  'h-1.5 w-1.5 rounded-full',
                  c.running
                    ? 'animate-pulse bg-success'
                    : c.exit_code != null && c.exit_code !== 0
                      ? 'bg-danger'
                      : 'bg-ink-4'
                )}
              />
              <span>{c.name}</span>
              {activeSessionKey && c.session_key === activeSessionKey ? (
                <span className="rounded bg-sky-500/20 px-1 text-[9px] text-accent">本对话</span>
              ) : (
                <span className="rounded bg-elevated px-1 text-[9px] text-ink-4">
                  {convTail(c.session_key)}
                </span>
              )}
            </button>
          ))}
          {sessions.map((s) => (
            <button
              key={s.key}
              onClick={() => setSelected(s.key)}
              className={cn(
                'flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs transition',
                selected === s.key
                  ? 'bg-contrast text-ink'
                  : 'text-ink-3 hover:bg-elevated hover:text-ink'
              )}
              title={`${s.user}@${s.host} · ${s.mode} · ${
                s.origin === 'desktop' ? '桌面发起' : 'agent 发起'
              }`}
            >
              <span
                className={cn(
                  'h-1.5 w-1.5 rounded-full',
                  s.alive && !s.closed ? 'bg-success' : 'bg-danger'
                )}
              />
              <span>{s.name}</span>
              {s.mode === 'exec' && (
                <span className="rounded bg-elevated px-1 text-[9px] text-ink-4">exec</span>
              )}
              {activeSessionKey && s.session_key === activeSessionKey && (
                <span className="rounded bg-sky-500/20 px-1 text-[9px] text-accent">本对话</span>
              )}
            </button>
          ))}
        </div>

        {badge && (
          <span className="ml-2 shrink-0 animate-pulse rounded bg-elevated px-1.5 py-0.5 text-[10px] text-ink-3">
            {badge.who === 'agent' ? 'agent 输入中' : '手动输入'}
          </span>
        )}

        <div className="ml-auto flex shrink-0 items-center gap-1">
          {selProcRunning && (
            <button
              onClick={stopProc}
              title="停止该常驻进程"
              className="flex items-center gap-1 rounded-lg bg-rose-600 px-2 py-1 text-[10px] font-medium text-white transition hover:bg-rose-500"
            >
              <Square className="h-2.5 w-2.5" /> 停止
            </button>
          )}
          <button
            onClick={() => setConnectOpen((v) => !v)}
            title="快速连接"
            className={cn(
              'rounded p-1.5 transition',
              connectOpen ? 'bg-contrast text-sky-300' : 'text-ink-3 hover:bg-elevated hover:text-ink'
            )}
          >
            <Plus className="h-4 w-4" />
          </button>
          <button
            onClick={() => setRefreshKey((k) => k + 1)}
            title="刷新会话/频道列表"
            className="rounded p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      {connectOpen && (
        <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel px-3 py-2 text-xs">
          {(
            [
              ['name', '别名*'],
              ['host', '主机'],
              ['port', '端口'],
              ['user', '用户'],
              ['token', 'Token/密码*', 'password'],
            ] as const
          ).map(([k, label, type]) => (
            <label key={k} className="flex items-center gap-1">
              <span className="text-ink-4">{label}</span>
              <input
                type={type === 'password' ? 'password' : 'text'}
                value={form[k]}
                onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))}
                className="w-28 rounded border border-line-strong bg-app px-1.5 py-1 text-ink outline-none focus:border-accent"
              />
            </label>
          ))}
          <label className="flex items-center gap-1">
            <span className="text-ink-4">模式</span>
            <select
              value={form.mode}
              onChange={(e) => setForm((f) => ({ ...f, mode: e.target.value }))}
              className="rounded border border-line-strong bg-app px-1.5 py-1 text-ink outline-none"
            >
              <option value="shell">shell（堡垒机）</option>
              <option value="exec">exec（普通服务器）</option>
            </select>
          </label>
          <button
            onClick={() => void onConnect()}
            disabled={connecting}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs font-medium transition',
              connecting
                ? 'cursor-wait bg-elevated text-ink-3'
                : 'bg-sky-600 text-white hover:bg-sky-500'
            )}
          >
            {connecting ? '连接中…' : '连接'}
          </button>
          {connectErr && <span className="text-danger">{connectErr}</span>}
          <span className="text-[10px] text-ink-4">
            与 agent 共用会话；连接后可直接在此终端操作，agent 也能使用
          </span>
        </div>
      )}

      <div className="relative min-h-0 flex-1">
        {selected ? (
          <>
            <div
              ref={hostRef}
              className="h-full w-full overflow-auto px-2 py-1"
              style={{ background: '#0a0a0a' }}
            />
            {deadReason && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-center pb-2">
                {selected === LOCAL ? (
                  <button
                    onClick={restartLocal}
                    className="pointer-events-auto rounded bg-elevated px-2 py-1 text-[10px] text-danger"
                  >
                    本地终端已退出（{deadReason}）— 点击重启
                  </button>
                ) : isLocalKey(selected) ? (
                  <span className="rounded bg-elevated px-2 py-1 text-[10px] text-danger">
                    频道已消失（{deadReason}）— 自动重连中
                  </span>
                ) : (
                  <span className="rounded bg-elevated px-2 py-1 text-[10px] text-danger">
                    会话已断开（{deadReason}）— 需重新连接（Token 不留存）
                  </span>
                )}
              </div>
            )}
            {!deadReason && !streamOk && (
              <div className="pointer-events-none absolute inset-x-0 top-0 flex justify-center pt-2">
                <span className="rounded bg-elevated px-2 py-1 text-[10px] text-ink-4">
                  连接终端流…
                </span>
              </div>
            )}
          </>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-ink-4">
            <p className="text-xs">无 SSH 会话</p>
            <p className="max-w-[24rem] text-center text-[10px] leading-relaxed">
              agent 通过 ssh 工具连接后这里会实时显示；也可以点右上角 + 用 Token 直连堡垒机
            </p>
          </div>
        )}
      </div>
    </section>
  )
}
