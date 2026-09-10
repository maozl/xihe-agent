import { spawn, type ChildProcess } from 'child_process'
import { promises as fs } from 'fs'
import { join } from 'path'
import { desktopDataDir } from './dataDir'

/** One-shot local command runner (the Run panel). One active run per workdir —
 *  two workspaces can run their own builds side by side; starting in a workdir
 *  supersedes only that workdir's previous run (interactive/concurrent
 *  processes are the local terminal's job, not this panel's). */

export type RunEvent =
  | { t: 'start'; id: number; command: string; cwd: string }
  | { t: 'out'; id: number; stream: 'out' | 'err'; chunk: string }
  | { t: 'exit'; id: number; code: number | null }

let pusher: ((ev: RunEvent) => void) | null = null
export function setRunPusher(p: (ev: RunEvent) => void): void {
  pusher = p
}

// workdir → the run started there most recently (removed on its exit).
const active = new Map<string, { id: number; child: ChildProcess }>()
let nextId = 1

/** Tree-kill: with shell:true the real command is a grandchild (cmd → program),
 *  so the shell's PID kill must fan out. /T /F is the Windows-native way. */
function killTree(child: ChildProcess): void {
  if (child.pid == null) return
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true })
  } else {
    child.kill('SIGKILL')
  }
}

export function stopRun(cwd: string): boolean {
  const run = active.get(cwd)
  if (!run) return false
  killTree(run.child)
  return true
}

/** Kill every active run (app quit — owned children must not outlive it). */
export function stopAllRuns(): void {
  for (const run of active.values()) killTree(run.child)
  active.clear()
}

export function startRun(command: string, cwd: string): number {
  // Supersede this workdir's previous run — its exit event still fires (stale
  // id on the renderer side is ignored) so the slot isn't left running.
  const prev = active.get(cwd)
  if (prev) killTree(prev.child)
  const id = nextId++
  const child = spawn(command, {
    shell: true,
    cwd,
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: '1' }, // line-live python output
  })
  active.set(cwd, { id, child })
  pusher?.({ t: 'start', id, command, cwd })
  child.stdout?.on('data', (b: Buffer) => {
    pusher?.({ t: 'out', id, stream: 'out', chunk: b.toString('utf8') })
  })
  child.stderr?.on('data', (b: Buffer) => {
    pusher?.({ t: 'out', id, stream: 'err', chunk: b.toString('utf8') })
  })
  child.on('error', (err) => {
    pusher?.({ t: 'out', id, stream: 'err', chunk: String(err) })
    pusher?.({ t: 'exit', id, code: null })
    if (active.get(cwd)?.id === id) active.delete(cwd)
  })
  child.on('close', (code) => {
    pusher?.({ t: 'exit', id, code })
    if (active.get(cwd)?.id === id) active.delete(cwd)
  })
  return id
}

// --- Command history (~/.xihe-desktop/run-history.json) -------------------
// { [workdir]: [command, …] } — most recent first, deduped, capped. The panel
// offers these for one-click / ↑ recall so parameterized commands never need
// retyping across sessions.

const HISTORY_CAP = 30

function historyPath(): string {
  return join(desktopDataDir(), 'run-history.json')
}

export async function loadRunHistory(): Promise<Record<string, string[]>> {
  try {
    const parsed = JSON.parse(await fs.readFile(historyPath(), 'utf8')) as Record<
      string,
      unknown
    >
    const out: Record<string, string[]> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (Array.isArray(v)) out[k] = v.filter((x): x is string => typeof x === 'string')
    }
    return out
  } catch {
    return {}
  }
}

export async function addRunHistory(workdir: string, command: string): Promise<void> {
  const all = await loadRunHistory()
  const list = all[workdir] ?? []
  const next = [command, ...list.filter((c) => c !== command)].slice(0, HISTORY_CAP)
  all[workdir] = next
  try {
    await fs.mkdir(desktopDataDir(), { recursive: true })
    const p = historyPath()
    await fs.writeFile(p + '.tmp', JSON.stringify(all, null, 2), 'utf8')
    await fs.rename(p + '.tmp', p)
  } catch {
    /* history is best-effort — never fail a run over it */
  }
}
