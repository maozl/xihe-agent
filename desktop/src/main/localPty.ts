import { existsSync } from 'fs'
import * as pty from 'node-pty'
import { homedir } from 'os'

/** ONE persistent local shell (ConPTY) shared by the terminal panel's 本地
 *  tab. Created lazily on first attach, survives panel close/tab switches
 *  (detach just stops the stream), killed on app quit or explicit reconnect. */

export type LocalPtyEvent =
  | { t: 'out'; chunk: string }
  | { t: 'exit'; reason: string }

let pusher: ((ev: LocalPtyEvent) => void) | null = null
export function setLocalPtyPusher(p: (ev: LocalPtyEvent) => void): void {
  pusher = p
}

const BACKLOG_CHARS = 16 * 1024

let term: pty.IPty | null = null
let backlog = ''
let attached = false
let alive = false

function preferredShell(): { file: string; args: string[] } {
  // PowerShell first (PSReadLine gives ↑ history / better editing); cmd is the
  // always-present fallback.
  const ps = joinPowershell()
  return ps ? { file: ps, args: ['-NoLogo'] } : { file: 'cmd.exe', args: [] }
}

function joinPowershell(): string | null {
  const p = `${process.env.SystemRoot ?? 'C:\\Windows'}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`
  return existsSync(p) ? p : null
}

/** Attach (creating on first call). `cwd` only seeds creation — an existing
 *  shell keeps its own working directory across re-attaches. */
export function attachLocalPty(cwd: string | undefined, cols: number, rows: number):
  { ok: true; backlog: string } | { ok: false; reason: string } {
  if (!term) {
    const { file, args } = preferredShell()
    try {
      term = pty.spawn(file, args, {
        name: 'xterm-256color',
        cols: Math.max(2, cols),
        rows: Math.max(2, rows),
        cwd: cwd && existsSync(cwd) ? cwd : homedir(),
        env: { ...process.env, TERM: 'xterm-256color' } as Record<string, string>,
      })
      alive = true
    } catch (err) {
      term = null
      return { ok: false, reason: String(err) }
    }
    term.onData((chunk) => {
      backlog = (backlog + chunk).slice(-BACKLOG_CHARS)
      if (attached) pusher?.({ t: 'out', chunk })
    })
    term.onExit(({ exitCode }) => {
      alive = false
      term = null
      backlog = ''
      if (attached) pusher?.({ t: 'exit', reason: `exit ${exitCode}` })
    })
  }
  attached = true
  return { ok: true, backlog }
}

export function detachLocalPty(): void {
  attached = false
}

export function writeLocalPty(data: string): void {
  try {
    term?.write(data)
  } catch {
    /* races a concurrent exit — the exit event reports it */
  }
}

export function resizeLocalPty(cols: number, rows: number): void {
  try {
    term?.resize(Math.max(2, cols), Math.max(2, rows))
  } catch {
    /* disposed mid-resize */
  }
}

/** Hard restart (panel's reconnect button): kills the shell so the next
 *  attach spawns a fresh one. */
export function killLocalPty(): void {
  try {
    term?.kill()
  } catch {
    /* already gone */
  }
  term = null
  alive = false
  backlog = ''
}

export function isLocalPtyAlive(): boolean {
  return alive
}
