// ServeSupervisor — the main process owns the `xihe serve` child lifecycle.
//
// Per [[0026]] the desktop ships xihe as a *built-in* agent: the user never
// runs `xihe serve` and never sees the word "serve" in the UI. This module is
// the single chokepoint that makes that true: it spawns / health-checks /
// restarts / kills the serve child, and reports process-level status to the
// renderer over the `xihe:status` IPC channel.
//
// Always-fresh is the core strategy. On start we probe `/health`; if a xihe is
// already reachable we KILL it (find its PID on the port, tree-kill) and spawn
// our own child. The desktop owns the xihe serve lifecycle and we always want
// the CURRENT code loaded — not whatever stale process a previous launch (or a
// manual `xihe serve`) left bound to the port (a stale serve means prompts.py /
// agent.py / tool edits never take effect, which looks like "my changes did
// nothing"). We only kill something the probe confirmed IS xihe (valid /health
// body), so a random app squatting the port is left alone and spawnChild
// surfaces the bind error. After the kill we wait for the port to free before
// spawning, else web.run_app on the still-bound port crashes the fresh child.
//
// Status contract lives here (canonical); preload + renderer/lib/desktop.ts
// redeclare the same shape across the bundle boundary (preload and the renderer
// browser bundle can't import from main).

import { spawn, type ChildProcess } from 'child_process'
import { join, isAbsolute } from 'path'
import {
  createWriteStream, statSync, renameSync, unlinkSync, type WriteStream,
} from 'fs'
import { killTree, findPidOnPort } from './proc'
import { desktopDataDir } from './dataDir'

export type XiheStatusState = 'starting' | 'running' | 'stopped' | 'errored' | 'not_found'

export interface XiheStatus {
  state: XiheStatusState
  /** true = we spawned it; false = adopted an external/dev serve. Drives
   *  whether we kill on quit and whether restart is our job. */
  owned: boolean
  version: string | null
  host: string
  port: number
  pid?: number | null
  /** human detail (esp. on errored). */
  message?: string
}

interface Health {
  ok: boolean
  version: string
}

// Readiness (just-spawned → first /health): tight poll, short fetch timeout,
// give up flagging `errored` after this many ms (liveness then keeps watching).
const READINESS_INTERVAL_MS = 400
const READINESS_TIMEOUT_MS = 30_000
const READINESS_FETCH_MS = 1_000
// Steady-state liveness: infrequent poll, longer fetch timeout (a half-open
// TCP slot can otherwise hang ~20s — AbortController caps it).
const LIVENESS_INTERVAL_MS = 6_000
const LIVENESS_FETCH_MS = 2_000
// Adopted serve dropped: re-probe this often (don't spawn our own — the dev is
// probably restarting it; racing for the port would double-spawn).
const ADOPT_REPROBE_MS = 3_000
// Backoff cadence (ms) for owned-child restart after an unexpected exit.
const BACKOFF_MS = [1_000, 2_000, 5_000]
// serve.log rotation threshold — at open, an oversized log is renamed to
// serve.log.old (dropping any previous .old) so the file can't grow forever.
const SERVE_LOG_MAX_BYTES = 5 * 1024 * 1024

export interface ServeSupervisorOptions {
  host?: string
  port?: number
  bin?: string
  onStatus?: (s: XiheStatus) => void
}

/** Where spawn stderr/stdout lands (~/.xihe-desktop/serve.log). The
 *  not_found status card offers to open it for the cmd.exe error text. */
export function serveLogPath(): string {
  return join(desktopDataDir(), 'serve.log')
}

export class ServeSupervisor {
  private readonly host: string
  private readonly port: number
  private readonly bin: string
  private readonly onStatus: (s: XiheStatus) => void
  private status: XiheStatus
  private child: ChildProcess | null = null
  private owned = false
  private stopped = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private backoffIdx = 0
  private readinessDeadline = 0
  private logStream: WriteStream | null = null

  constructor(opts: ServeSupervisorOptions = {}) {
    this.host = opts.host ?? '127.0.0.1'
    this.port = opts.port ?? 7788
    this.bin = opts.bin ?? process.env.XIHE_BIN ?? 'xihe'
    this.onStatus = opts.onStatus ?? (() => {})
    this.status = {
      state: 'starting',
      owned: false,
      version: null,
      host: this.host,
      port: this.port,
      pid: null,
      message: '正在启动xihe…',
    }
  }

  /** Latest status snapshot — the renderer pulls this on mount to cover the
   *  pre-`did-finish-load` window where pushes may be dropped. */
  snapshot(): XiheStatus {
    return { ...this.status }
  }

  /** Base URL for direct HTTP calls into the serve child (browser-panel snap
   *  uses the /browser/* endpoints; the renderer stays on serveClient). */
  baseUrl(): string {
    return `http://${this.host}:${this.port}`
  }

  /** Entry point. Fire-and-forget from `app.whenReady` (never awaited there) —
   *  the initial probe + spawn run asynchronously. */
  start(): void {
    void this.boot()
  }

  /** Restart the serve child so config.yaml edits take effect (serve reads
   *  config only at boot). Called from the `serve:restart` IPC when the user
   *  hits 重启xihe in the config panel. Distinct from stop() (final shutdown):
   *  we kill the current child WITHOUT flipping `stopped`, and set owned=false
   *  first so the about-to-fire exit handler bails (`if (!this.owned) return`)
   *  instead of racing boot() with its own scheduleRestart. boot() then probes
   *  (kills anything still on the port), waits for it to free, and spawns a
   *  fresh child that re-reads config.yaml. */
  restart(): void {
    if (this.stopped) return
    this.clearTimer()
    this.owned = false
    if (this.child) {
      try {
        killTree(this.child.pid)
      } catch {
        /* best-effort — boot() will kill whatever is still on the port */
      }
      this.child = null
    }
    this.emit('starting', { message: '正在重启xihe以应用配置…', version: null })
    void this.boot()
  }

  private async boot(): Promise<void> {
    // Kill any confirmed-xihe left on our port so we spawn fresh (see header),
    // then wait for the bind to release before spawning our own child.
    const h = await this.probe(READINESS_FETCH_MS)
    if (this.stopped) return
    if (h) {
      this.emit('starting', { message: '重启xihe以加载最新代码…', version: null })
      this.killExistingServe()
      await this.waitForPortFree()
      if (this.stopped) return
    }
    this.spawnChild()
  }

  /** Kill whatever xihe serve holds our port (best-effort). Only called after
   *  /health confirmed a xihe is there, so we never kill an unrelated app. */
  private killExistingServe(): void {
    const pid = findPidOnPort(this.port)
    if (pid) killTree(pid)
  }

  /** Poll /health until the killed serve stops answering (port released), so the
   *  fresh child doesn't hit web.run_app on a still-bound port. Bounded — if the
   *  old process is stubborn we spawn anyway and let spawnChild's restart
   *  backoff handle a transient bind error. */
  private async waitForPortFree(): Promise<void> {
    const deadline = Date.now() + 5_000
    while (Date.now() < deadline) {
      const h = await this.probe(READINESS_FETCH_MS)
      if (!h) return
      await new Promise((r) => setTimeout(r, 200))
    }
  }

  private spawnChild(): void {
    if (this.stopped) return
    this.owned = true
    this.emit('starting', { message: '正在启动xihe…', version: null })

    const args = ['serve', '--host', this.host, '--port', String(this.port)]
    // On Windows, spawning `xihe` needs PATHEXT resolution → shell. An absolute
    // XIHE_BIN is spawned directly (predictable, no transient cmd.exe).
    const useShell = process.platform === 'win32' && !isAbsolute(this.bin)
    let child: ChildProcess
    try {
      child = spawn(this.bin, args, {
        shell: useShell,
        windowsHide: true,
        // POSIX: new process group so `kill(-pid)` reaches any subprocess;
        // Windows kills via `taskkill /T` instead (detached irrelevant there).
        detached: process.platform !== 'win32',
        env: { ...process.env },
      })
    } catch (e) {
      this.emit('errored', { message: `无法启动xihe：${errMsg(e)}` })
      this.scheduleRestart()
      return
    }
    this.child = child
    this.pipeLogs(child)

    // With shell:true a missing binary does NOT fire 'error' — cmd.exe runs,
    // prints "'xihe' 不是内部或外部命令" to stderr, exits 1. Sniff for that
    // so the exit handler can distinguish "xihe 未安装" (terminal, no retry)
    // from a crash worth backing off on.
    let binMissing = false
    child.stderr?.on('data', (d: Buffer | string) => {
      const s = typeof d === 'string' ? d : d.toString('utf8')
      if (s.includes('不是内部或外部命令') || s.includes('is not recognized')) {
        binMissing = true
      }
    })

    child.on('exit', (code, signal) => {
      if (this.stopped) return // expected — we killed it on quit
      if (!this.owned) return // not ours to manage
      this.child = null
      if (binMissing) {
        // Retrying can't help until the user installs xihe — park in not_found
        // (restart()/a fresh launch re-probes PATH).
        this.emit('not_found', this.notFoundMessage())
        return
      }
      this.emit('stopped', {
        message: `xihe进程退出（code=${code} signal=${signal ?? ''}），将重启…`,
        version: null,
      })
      this.scheduleRestart()
    })
    child.on('error', (e) => {
      if (this.stopped) return
      this.child = null
      const m = errMsg(e)
      if (/ENOENT|not found/i.test(m)) {
        this.emit('not_found', this.notFoundMessage())
        return
      }
      this.emit('errored', { message: `xihe启动失败：${m}` })
      this.scheduleRestart()
    })

    // Readiness: poll /health until it answers or the deadline passes.
    this.readinessDeadline = Date.now() + READINESS_TIMEOUT_MS
    this.scheduleReadiness()
  }

  private notFoundMessage(): string {
    return this.bin === 'xihe'
      ? '未找到 xihe 命令 — 先 pip install -e . （或设 XIHE_BIN 指向可执行文件），再点「重启xihe」'
      : `XIHE_BIN 指向的 ${this.bin} 不存在 — 修正环境变量后点「重启xihe」`
  }

  private scheduleReadiness(): void {
    this.setTimer(() => void this.readinessTick(), READINESS_INTERVAL_MS)
  }

  private async readinessTick(): Promise<void> {
    if (this.stopped) return
    const h = await this.probe(READINESS_FETCH_MS)
    if (this.stopped) return
    if (h) {
      this.backoffIdx = 0
      this.emit('running', { version: h.version, message: undefined })
      this.scheduleLiveness()
      return
    }
    if (Date.now() > this.readinessDeadline) {
      // Gave up waiting for readiness. Don't kill — hand off to liveness: if the
      // child is just slow it'll flip to running; if it died the exit handler
      // already scheduled a restart.
      this.emit('errored', {
        message: `xihe启动超时（${READINESS_TIMEOUT_MS / 1000}s 内未就绪）`,
      })
      this.scheduleLiveness()
      return
    }
    this.scheduleReadiness()
  }

  private scheduleLiveness(): void {
    this.setTimer(() => void this.livenessTick(), LIVENESS_INTERVAL_MS)
  }

  private async livenessTick(): Promise<void> {
    if (this.stopped) return
    const h = await this.probe(LIVENESS_FETCH_MS)
    if (this.stopped) return
    if (h) {
      this.backoffIdx = 0
      if (this.status.state !== 'running') {
        this.emit('running', { version: h.version, message: undefined })
      }
      this.scheduleLiveness()
      return
    }
    // /health failed.
    if (this.owned) {
      // Owned but unresponsive. If the process is still alive it may recover
      // (and if it's actually dead, the exit handler owns the restart).
      if (this.child && !this.child.killed && this.status.state !== 'stopped') {
        this.emit('stopped', { message: 'xihe未响应 /health，等待恢复…', version: null })
      }
      this.scheduleLiveness()
    } else {
      // Adopted external serve dropped — don't spawn over the dev's port; just
      // re-probe slowly until it comes back.
      if (this.status.state !== 'stopped') {
        this.emit('stopped', { message: 'xihe未运行，等待…', version: null })
      }
      this.setTimer(() => void this.adoptReprobe(), ADOPT_REPROBE_MS)
    }
  }

  private async adoptReprobe(): Promise<void> {
    if (this.stopped) return
    const h = await this.probe(READINESS_FETCH_MS)
    if (this.stopped) return
    if (h) {
      this.emit('running', { version: h.version, message: undefined })
      this.scheduleLiveness()
    } else {
      this.setTimer(() => void this.adoptReprobe(), ADOPT_REPROBE_MS)
    }
  }

  private scheduleRestart(): void {
    if (this.stopped) return
    const delay = BACKOFF_MS[Math.min(this.backoffIdx, BACKOFF_MS.length - 1)]
    this.backoffIdx += 1
    this.setTimer(() => {
      if (this.stopped) return
      this.spawnChild()
    }, delay)
  }

  /** Idempotent. Called from `before-quit`. Tree-kills the owned child; leaves
   *  an adopted external serve alone. */
  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.clearTimer()
    if (this.owned && this.child) {
      killTree(this.child.pid)
      this.child = null
    }
    this.logStream?.end()
    this.logStream = null
  }

  private setTimer(fn: () => void, ms: number): void {
    this.clearTimer()
    this.timer = setTimeout(fn, ms)
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private emit(state: XiheStatusState, patch: Partial<XiheStatus> = {}): void {
    this.status = {
      ...this.status,
      state,
      owned: this.owned,
      pid: this.owned ? (this.child?.pid ?? null) : null,
      ...patch,
    }
    this.onStatus(this.snapshot())
  }

  /** GET /health with a hard timeout. Validates the body shape (not just HTTP
   *  200) so a different app squatting the port isn't mistaken for xihe. */
  private async probe(timeoutMs: number): Promise<Health | null> {
    const url = `http://${this.host}:${this.port}/health`
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      const r = await fetch(url, { signal: ctrl.signal })
      if (!r.ok) return null
      const j = (await r.json()) as unknown
      if (
        j &&
        typeof j === 'object' &&
        (j as { ok?: unknown }).ok === true &&
        typeof (j as { version?: unknown }).version === 'string'
      ) {
        return { ok: true, version: (j as { version: string }).version }
      }
      return null
    } catch {
      return null // unreachable / timeout / malformed — all "not up"
    } finally {
      clearTimeout(t)
    }
  }

  /** Append the child's stdout/stderr to ~/.xihe-desktop/serve.log so a
   *  "xihe wouldn't start" report stays diagnosable. Best-effort.
   *  The stream is opened once per app run and never rotated mid-run; the
   *  size check at open keeps a rolling two-file window instead of an
   *  unbounded append across months of launches. */
  private pipeLogs(child: ChildProcess): void {
    try {
      if (!this.logStream) this.logStream = this.openLogStream()
      child.stdout?.on('data', (d: Buffer | string) => this.logStream?.write(d))
      child.stderr?.on('data', (d: Buffer | string) => this.logStream?.write(d))
    } catch {
      /* logging is best-effort */
    }
  }

  private openLogStream(): WriteStream {
    const path = serveLogPath()
    try {
      if (statSync(path).size > SERVE_LOG_MAX_BYTES) {
        try {
          unlinkSync(`${path}.old`)
        } catch {
          /* no previous rotation — fine */
        }
        renameSync(path, `${path}.old`)
      }
    } catch {
      /* fresh file / stat race — just append */
    }
    const s = createWriteStream(path, { flags: 'a' })
    s.write(`\n--- xihe serve ${new Date().toISOString()} ---\n`)
    return s
  }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
