// Cross-platform process-tree kill + port→PID lookup, used by ServeSupervisor
// (xihe serve).
//
// Why not `child.kill()`: on win32 Node only TerminateProcess'es the single
// PID. xihe's console_script trampoline spawns python as a child (would be
// orphaned), so a process-tree kill is required to actually clean up.

import { spawnSync, execSync } from 'child_process'

/** Best-effort kill of an entire process tree rooted at `pid`. Never throws.
 *  - Windows: `taskkill /PID <pid> /T /F` walks the tree.
 *  - POSIX:   `process.kill(-pid, 'SIGTERM')` (callers spawn `detached:true`
 *             so the child leads its own process group). */
export function killTree(pid: number | null | undefined): void {
  if (pid == null) return
  try {
    if (process.platform === 'win32') {
      // Synchronous on purpose: killTree runs from before-quit, and a
      // fire-and-forget taskkill is torn down with the exiting Electron
      // process before it finishes the tree — orphaning the serve child
      // (port still bound, ~/.xihe-agent still locked). Blocking ~1s at
      // quit is the price of a reliable kill.
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
        windowsHide: true,
        timeout: 10_000,
      })
    } else {
      process.kill(-pid, 'SIGTERM')
    }
  } catch {
    /* best effort — process may already be gone */
  }
}

/** Resolve the PID listening on `port`, or null. Used by ServeSupervisor to
 *  find a stale serve left on the port so it can kill+replace it with a fresh
 *  child. Best-effort — any error (command missing, no listener) → null, which
 *  the caller treats as "nothing to kill".
 *  - Windows: parse `netstat -ano` for the LISTENING line on `:port`. The port
 *    is matched with a word boundary so port 778 isn't confused with 7788.
 *  - POSIX:   `lsof -ti tcp:<port>` prints the PID directly.
 *
 *  Synchronous on purpose everywhere, not just before-quit: an async variant
 *  was tried for the boot/restart paths and regressed (the lookup silently
 *  failed → stale serve never killed → every fresh child died on bind 10048).
 *  The ~1s main-process block on 重启xihe is the price of reliability. */
export function findPidOnPort(port: number): number | null {
  try {
    if (process.platform === 'win32') {
      const out = execSync('netstat -ano', { windowsHide: true, encoding: 'utf8' })
      return parseNetstat(out, port)
    }
    const out = execSync(`lsof -ti tcp:${port}`, { encoding: 'utf8' })
    const first = Number(out.trim().split('\n')[0])
    return Number.isFinite(first) && first > 0 ? first : null
  } catch {
    return null
  }
}

function parseNetstat(out: string, port: number): number | null {
  const portRe = new RegExp(`:${port}\\b`)
  for (const line of out.split(/\r?\n/)) {
    if (!line.includes('LISTENING') || !portRe.test(line)) continue
    const m = line.match(/(\d+)\s*$/)
    if (m) return Number(m[1])
  }
  return null
}
