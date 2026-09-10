import { execFile } from 'child_process'
import { existsSync } from 'fs'
import { dirname, isAbsolute, join, relative } from 'path'
import { promisify } from 'util'

const execFileAsync = promisify(execFile)

export interface GitStatusEntry {
  /** Absolute, forward-slash-normalized (renderer normalizes tree paths the
   *  same way before comparing). */
  path: string
  /** Porcelain XY codes (index / worktree). */
  x: string
  y: string
}

export type GitStatus =
  | { ok: true; repoRoot: string; branch: string; entries: GitStatusEntry[] }
  | { ok: false; reason: 'not-repo' | 'no-git' | 'error' }

// Cap so a pathological repo can't blow IPC / the renderer's index build.
const MAX_ENTRIES = 5000

/** Walk up from *start* to the containing repo (a `.git` dir, or the file a
 *  worktree/submodule uses to point at it). null when outside any repo. */
function findRepoRoot(start: string): string | null {
  let dir = start
  for (;;) {
    if (existsSync(join(dir, '.git'))) return dir
    const parent = dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

/** `## main...origin/main [ahead 1]` / `## No commits yet on main` → "main". */
function parseBranch(header: string): string {
  const s = header.replace(/^##\s*/, '')
  if (s.startsWith('No commits yet on ')) return s.slice(17).trim()
  return s.split('...')[0].trim() || s.trim()
}

/** Async `git status` — event-driven (fsVersion bumps / manual refresh), never
 *  polled, with a hard timeout so a hung git can't wedge the caller. MUST stay
 *  async: a sync spawn would block main's IPC loop and stall every concurrent
 *  renderer call (e.g. the tree's own listDir) for the whole git run. */
export async function gitStatus(workdir: string): Promise<GitStatus> {
  const repoRoot = findRepoRoot(workdir)
  if (!repoRoot) return { ok: false, reason: 'not-repo' }

  let stdout: string
  try {
    const res = await execFileAsync(
      'git',
      [
        '-C',
        repoRoot,
        // --no-optional-locks: a background status read shouldn't touch the
        // user's index.lock (contention with their own git commands).
        '--no-optional-locks',
        'status',
        '--porcelain=v1',
        '-z',
        '--untracked-files=all',
        '-b',
      ],
      { windowsHide: true, timeout: 10_000, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8' }
    )
    stdout = res.stdout
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code
    return { ok: false, reason: code === 'ENOENT' ? 'no-git' : 'error' }
  }

  const rootPosix = repoRoot.replace(/\\/g, '/')
  const parts = (stdout || '').split('\0')
  const entries: GitStatusEntry[] = []
  let branch = ''
  for (let i = 0; i < parts.length; i++) {
    const rec = parts[i]
    if (!rec) continue
    if (rec.startsWith('#')) {
      if (!branch) branch = parseBranch(rec)
      continue
    }
    if (rec.length < 4) continue
    const x = rec[0]
    const y = rec[1]
    entries.push({ path: rootPosix + '/' + rec.slice(3), x, y })
    // -z: rename/copy records carry the orig path as an extra NUL field.
    if (x === 'R' || x === 'C' || y === 'R' || y === 'C') i++
    if (entries.length >= MAX_ENTRIES) break
  }
  return { ok: true, repoRoot, branch, entries }
}

export type GitHeadFile =
  | { ok: true; content: string | null } // null = untracked / not in HEAD
  | { ok: false; reason: 'not-repo' | 'no-git' | 'too-large' | 'error' }

// Matches the renderer editor's 1 MiB read cap — bigger files don't open in
// Monaco, so their HEAD text would never be used.
const HEAD_MAX = 1024 * 1024

/** `git show HEAD:<file>` — the committed base for the editor's gutter change
 *  marks. Async for the same reason as gitStatus: a sync spawn would stall
 *  main's IPC loop. */
export async function gitHeadFile(absPath: string): Promise<GitHeadFile> {
  const repoRoot = findRepoRoot(dirname(absPath))
  if (!repoRoot) return { ok: false, reason: 'not-repo' }
  const rel = relative(repoRoot, absPath).replace(/\\/g, '/')
  if (!rel || rel.startsWith('..') || isAbsolute(rel)) return { ok: false, reason: 'error' }
  try {
    const res = await execFileAsync(
      'git',
      ['-C', repoRoot, '--no-optional-locks', 'show', `HEAD:${rel}`],
      { windowsHide: true, timeout: 10_000, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8' }
    )
    if (res.stdout.length > HEAD_MAX) return { ok: false, reason: 'too-large' }
    return { ok: true, content: res.stdout }
  } catch (err) {
    const e = err as { code?: unknown; stderr?: string }
    if (e.code === 'ENOENT') return { ok: false, reason: 'no-git' }
    // Untracked file, or a repo whose HEAD has no commits yet — either way the
    // whole file is "new", i.e. the base is empty. Other fatals stay errors.
    if (/exists on disk, but not in|bad revision|ambiguous argument|Invalid object name/i.test(e.stderr ?? '')) {
      return { ok: true, content: null }
    }
    return { ok: false, reason: 'error' }
  }
}
