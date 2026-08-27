import { app, shell, BrowserWindow, dialog, ipcMain, nativeTheme } from 'electron'
import { join, isAbsolute, dirname, basename, relative } from 'path'
import { promises as fs } from 'fs'
import { ServeSupervisor, serveLogPath, type XiheStatus, type XiheStatusState } from './serve'
import { BrowserPanelController } from './browserPanel'
import { readXiheConfig, writeXiheConfig, type XiheConfigPatch } from './xiheConfig'
import { desktopDataDir } from './dataDir'

// Chromium 启动噪音(均无害:GPU 硬解回退软解;后台公网探测在气隙必 reset),
// app 实际走 ws://localhost,与此无关,仅压日志。对应日志签名 kTransientFailure /
// net_error -101。全部须在 app ready 前设。(GPU 仍残留则再加 disable-gpu。)
app.disableHardwareAcceleration()
app.commandLine.appendSwitch('disable-background-networking')
app.commandLine.appendSwitch('disable-component-update')
app.commandLine.appendSwitch('disable-client-side-phishing-detection')
app.commandLine.appendSwitch('disable-sync')
app.commandLine.appendSwitch(
  'disable-features',
  'Translate,MediaRouter,OptimizationHints,OptimizationGuideModelDownloading,InterestFeedContentSuggestions'
)

// Workspace IPC — pure-desktop file tree (no serve involvement). Writes are
// sandboxed to a workspace root via resolveInsideWorkspace() below; reads stay
// unsandboxed (local single-user app, absolute-path validation only).

// Directories that bloat a code tree without adding value; hidden regardless of
// the dotfile decision below (node_modules/.git alone dwarf everything else).
const SKIP_DIRS = new Set([
  'node_modules', '.git', '.svn', '.hg', 'dist', 'build', 'out',
  '.cache', '.next', '.vite', '.turbo', '__pycache__', '.pytest_cache',
])

const MAX_READ = 1024 * 1024 // 1 MiB — larger files are truncated, still readable
const SNIFF = 8192 // bytes scanned for a NUL to detect binary files

/** True if the buffer looks binary (a NUL byte in the first SNIFF bytes). */
function isBinary(buf: Buffer): boolean {
  const len = Math.min(buf.length, SNIFF)
  for (let i = 0; i < len; i++) if (buf[i] === 0) return true
  return false
}

// Plaintext workspace store. Mirrors the ~/.xihe-agent/ convention so it sits
// next to the agent's data root and is easy to find/edit by hand:
//   ~/.xihe-desktop/workspaces.json
//   { "workspaces": [{id,name,workdir}], "convWorkspace": {convId: wsId} }
function workspaceStoreDir(): string {
  return desktopDataDir()
}
function workspaceStorePath(): string {
  return join(workspaceStoreDir(), 'workspaces.json')
}

// Desktop-local settings, same plaintext convention:
//   ~/.xihe-desktop/settings.json  { "theme": "dark" | "light" | "system" }
// Theming is a desktop capability — config.yaml carries NO theme keys by
// design; the resolved light/dark is PUSHED to xihe as browser runtime state.
type ThemeMode = 'dark' | 'light' | 'system'
const THEME_MODES: readonly string[] = ['dark', 'light', 'system']
let currentTheme: ThemeMode = 'dark'

function settingsStorePath(): string {
  return join(workspaceStoreDir(), 'settings.json')
}

async function readSettings(): Promise<{ theme: ThemeMode }> {
  try {
    const parsed = JSON.parse(
      await fs.readFile(settingsStorePath(), 'utf8')
    ) as { theme?: unknown }
    const t = parsed?.theme
    return {
      theme: typeof t === 'string' && THEME_MODES.includes(t) ? (t as ThemeMode) : 'dark'
    }
  } catch {
    return { theme: 'dark' }
  }
}

async function writeSettings(theme: ThemeMode): Promise<boolean> {
  try {
    await fs.mkdir(workspaceStoreDir(), { recursive: true })
    const filePath = settingsStorePath()
    const tmp = filePath + '.tmp'
    await fs.writeFile(tmp, JSON.stringify({ theme }, null, 2), 'utf8')
    await fs.rename(tmp, filePath)
    return true
  } catch {
    return false
  }
}

// Workspace-root sandbox — confines DESTRUCTIVE writes to a known workspace.
// Roots are the authoritative workdirs from workspaces.json, realpath-resolved
// on each call so add/remove are honoured. realpath() on both sides defeats
// symlink/junction escapes; a not-yet-existing target resolves via its parent.

type FsFailReason = 'outsideWorkspace' | 'exists' | 'notFound' | 'io'
export type FsResult = { ok: true } | { ok: false; reason: FsFailReason }

/** Realpath-resolved workdirs of every workspace (existing dirs only). */
async function allowedRoots(): Promise<string[]> {
  try {
    const raw = await fs.readFile(workspaceStorePath(), 'utf8')
    const parsed = JSON.parse(raw) as { workspaces?: { workdir?: unknown }[] }
    if (!Array.isArray(parsed.workspaces)) return []
    const roots: string[] = []
    for (const w of parsed.workspaces) {
      const wd = w?.workdir
      if (typeof wd !== 'string' || !wd) continue
      try {
        roots.push(await fs.realpath(wd))
      } catch {
        // root itself missing — can't write under it; skip
      }
    }
    return roots
  } catch {
    return []
  }
}

/** `relative(root, real)` not escaping upward and not absolute → inside. */
function isInsideRoot(root: string, real: string): boolean {
  const rel = relative(root, real)
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}

/** Resolve *target* to a canonical real path and confirm it's under some
 *  workspace root. Existing → realpath; not-yet-existing (create / rename
 *  destination) → realpath(parent) + basename. Never throws — returns a
 *  typed failure the handler passes straight to the renderer. */
async function resolveInsideWorkspace(
  target: string
): Promise<{ ok: true; real: string } | { ok: false; reason: FsFailReason }> {
  if (typeof target !== 'string' || !isAbsolute(target)) {
    return { ok: false, reason: 'outsideWorkspace' }
  }
  let real: string
  try {
    real = await fs.realpath(target)
  } catch {
    try {
      const parentReal = await fs.realpath(dirname(target))
      real = join(parentReal, basename(target))
    } catch {
      return { ok: false, reason: 'io' }
    }
  }
  const roots = await allowedRoots()
  if (roots.length === 0 || !roots.some((r) => isInsideRoot(r, real))) {
    return { ok: false, reason: 'outsideWorkspace' }
  }
  return { ok: true, real }
}

const fsFail = (reason: FsFailReason): FsResult => ({ ok: false, reason })

function reasonOf(e: unknown, missing: FsFailReason = 'io'): FsFailReason {
  const code = (e as NodeJS.ErrnoException | undefined)?.code
  if (code === 'ENOENT') return 'notFound'
  if (code === 'EEXIST' || code === 'ENOTEMPTY') return 'exists'
  return missing
}

// xihe serve supervisor — the main process owns the `xihe serve` child
// lifecycle. See ./serve.ts and [[0026]]. The renderer reads process-level
// status via `xihe:status`.
let supervisor: ServeSupervisor | null = null

// Browser-panel snap bridge (see ./browserPanel.ts). Created in whenReady —
// the supervisor isn't up yet at createWindow time, hence the lazy getters.
let browserPanel: BrowserPanelController | null = null

/** Resolved light/dark for the current theme mode. must run AFTER the
 *  themeSource assignment — shouldUseDarkColors reflects it synchronously. */
function effectiveDark(): boolean {
  return currentTheme === 'dark' ||
    (currentTheme === 'system' && nativeTheme.shouldUseDarkColors)
}

/** Best-effort push of the resolved appearance to xihe, so CDP Chrome
 *  launches matching the app (xihe persists it as browser runtime state, not
 *  config). Skipped when serve isn't up yet — the serve-ready transition
 *  re-pushes with the latest value. */
function pushAppearance(): void {
  const base = supervisor?.baseUrl() ?? 'http://127.0.0.1:7788'
  void fetch(`${base}/browser/appearance`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dark: effectiveDark() }),
  }).catch(() => {
    // serve down (boot race / restart) — not an error; next transition re-pushes
  })
}

function registerIpc(): void {
  ipcMain.handle('desktop:ping', () => 'pong')

  // xihe:status pull — covers the window where a `running` push landed before
  // did-finish-load (pushed events aren't reliably delivered until then).
  ipcMain.handle('xihe:status', () => supervisor?.snapshot() ?? null)

  // Missing/corrupt file → empty store; renderer never touches the fs.
  ipcMain.handle('workspace:load', async () => {
    try {
      const raw = await fs.readFile(workspaceStorePath(), 'utf8')
      const parsed = JSON.parse(raw) as { workspaces?: unknown; convWorkspace?: unknown }
      const workspaces = Array.isArray(parsed.workspaces)
        ? parsed.workspaces.filter(
            (w): w is { id: string; name: string; workdir: string } =>
              !!w &&
              typeof w.id === 'string' &&
              typeof w.name === 'string' &&
              typeof w.workdir === 'string'
          )
        : []
      const convWorkspace: Record<string, string> = {}
      if (parsed.convWorkspace && typeof parsed.convWorkspace === 'object') {
        for (const [k, v] of Object.entries(parsed.convWorkspace as Record<string, unknown>)) {
          if (typeof v === 'string') convWorkspace[k] = v
        }
      }
      return { workspaces, convWorkspace }
    } catch {
      return { workspaces: [], convWorkspace: {} }
    }
  })

  // Atomic write (tmp + rename) so a crash mid-write can't corrupt the file.
  ipcMain.handle('workspace:save', async (_e, data: unknown) => {
    if (!data || typeof data !== 'object') return false
    try {
      await fs.mkdir(workspaceStoreDir(), { recursive: true })
      const filePath = workspaceStorePath()
      const tmp = filePath + '.tmp'
      await fs.writeFile(tmp, JSON.stringify(data, null, 2), 'utf8')
      await fs.rename(tmp, filePath)
      return true
    } catch {
      return false
    }
  })

  // xihe config — panel edits ~/.xihe-agent/config.yaml directly (line-patch,
  // comment-preserving). serve reads config.yaml at boot, so edits need
  // serve:restart to take effect.
  ipcMain.handle('xiheConfig:load', async () => readXiheConfig())
  ipcMain.handle('xiheConfig:save', async (_e, data: XiheConfigPatch) => writeXiheConfig(data))
  ipcMain.handle('serve:restart', async () => {
    supervisor?.restart()
    return !!supervisor
  })

  // Open serve.log with the OS default app — the not_found card offers it so
  // the user can read the raw spawn error. Touched into existence first: a
  // spawn that died before any output would leave nothing to open.
  ipcMain.handle('shell:openServeLog', async () => {
    try {
      const p = serveLogPath()
      await fs.mkdir(dirname(p), { recursive: true })
      await (await fs.open(p, 'a')).close()
      const err = await shell.openPath(p)
      return !err
    } catch {
      return false
    }
  })

  // Desktop-local settings + theme apply. Click-to-apply: nativeTheme flips
  // the renderer's prefers-color-scheme immediately, settings.json persists
  // across launches, and the resolved light/dark is pushed to xihe so the
  // browser panel's Chrome matches.
  ipcMain.handle('settings:load', async () => readSettings())
  ipcMain.handle('app:setTheme', async (_e, theme: unknown) => {
    if (typeof theme !== 'string' || !THEME_MODES.includes(theme)) {
      return { ok: false }
    }
    currentTheme = theme as ThemeMode
    nativeTheme.themeSource = currentTheme
    const persisted = await writeSettings(currentTheme)
    pushAppearance()
    return { ok: true, theme: currentTheme, persisted }
  })

  // Browser panel: renderer reports the placeholder rect (CSS px, relative to
  // the content area) or null to un-track. All Win32 work is in serve; main
  // only does DPI math and event wiring (browserPanel.ts).
  ipcMain.handle('browser:setSnapRegion', (_e, rect: unknown, mode?: unknown) => {
    browserPanel?.setSnapRegion(
      (rect as never) ?? null,
      mode === 'float' ? 'float' : 'hide'
    )
    return true
  })
  ipcMain.handle('browser:setPanelActive', (_e, active: unknown) => {
    browserPanel?.setPanelActive(active === true)
    return true
  })

  ipcMain.handle('dialog:openDirectory', async () => {
    const res = await dialog.showOpenDialog({
      title: '选择工作空间目录',
      properties: ['openDirectory', 'createDirectory'],
    })
    if (res.canceled || res.filePaths.length === 0) return null
    return res.filePaths[0]
  })

  // Returns the absolute child `path` ready-made: the renderer cannot import
  // node's `path` and would otherwise have to do OS-separator-aware joins
  // (Windows backslashes) itself — a whole class of bugs avoided for free.
  ipcMain.handle('fs:listDir', async (_e, dirPath: unknown) => {
    if (typeof dirPath !== 'string' || !isAbsolute(dirPath)) return []
    try {
      const entries = await fs.readdir(dirPath, { withFileTypes: true })
      const out: { name: string; dir: boolean; path: string }[] = []
      for (const ent of entries) {
        // Keep dotfiles (.gitignore/.env/.editorconfig…) — useful in a code
        // workspace; the heavy noise is caught by SKIP_DIRS above.
        if (SKIP_DIRS.has(ent.name)) continue
        out.push({ name: ent.name, dir: ent.isDirectory(), path: join(dirPath, ent.name) })
      }
      out.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1))
      return out
    } catch {
      return [] // unreadable / not a dir / missing — render as empty
    }
  })

  ipcMain.handle('fs:readFile', async (_e, filePath: unknown) => {
    if (typeof filePath !== 'string' || !isAbsolute(filePath)) {
      return { ok: false, reason: 'invalid' as const }
    }
    try {
      const stat = await fs.stat(filePath)
      if (!stat.isFile()) return { ok: false, reason: 'notFile' as const }
      const size = stat.size

      // Binary guard: sniff the leading bytes once.
      const sniff = Buffer.alloc(Math.min(SNIFF, size))
      const fd0 = await fs.open(filePath, 'r')
      try {
        await fd0.read(sniff, 0, sniff.length, 0)
      } finally {
        await fd0.close()
      }
      if (isBinary(sniff)) return { ok: false, reason: 'binary' as const, size }

      if (size > MAX_READ) {
        const buf = Buffer.alloc(MAX_READ)
        const fd = await fs.open(filePath, 'r')
        try {
          await fd.read(buf, 0, MAX_READ, 0)
        } finally {
          await fd.close()
        }
        return { ok: true as const, content: buf.toString('utf8'), size, truncated: true }
      }
      const content = await fs.readFile(filePath, 'utf8')
      return { ok: true as const, content, size, truncated: false }
    } catch {
      return { ok: false, reason: 'notFound' as const }
    }
  })

  // Destructive writes (sandboxed to a workspace root). The renderer can't
  // import node `path`, so create/rename take parent+name / path+newName and
  // main joins — the renderer never builds a path itself.

  // Atomic: tmp + rename.
  ipcMain.handle('fs:writeFile', async (_e, filePath: unknown, content: unknown) => {
    if (typeof filePath !== 'string' || typeof content !== 'string') return fsFail('io')
    const r = await resolveInsideWorkspace(filePath)
    if (!r.ok) return r
    try {
      await fs.mkdir(dirname(r.real), { recursive: true })
      const tmp = r.real + '.tmp'
      await fs.writeFile(tmp, content, 'utf8')
      await fs.rename(tmp, r.real)
      return { ok: true }
    } catch (e) {
      return fsFail(reasonOf(e))
    }
  })

  // Create an empty file named *name* inside *parentDir*.
  ipcMain.handle('fs:createFile', async (_e, parentDir: unknown, name: unknown) => {
    if (typeof parentDir !== 'string' || typeof name !== 'string' || !name.trim()) return fsFail('io')
    const r = await resolveInsideWorkspace(join(parentDir, name.trim()))
    if (!r.ok) return r
    try {
      await fs.writeFile(r.real, '', 'utf8')
      return { ok: true }
    } catch (e) {
      return fsFail(reasonOf(e))
    }
  })

  // Create a directory named *name* inside *parentDir* (recursive).
  ipcMain.handle('fs:createDir', async (_e, parentDir: unknown, name: unknown) => {
    if (typeof parentDir !== 'string' || typeof name !== 'string' || !name.trim()) return fsFail('io')
    const r = await resolveInsideWorkspace(join(parentDir, name.trim()))
    if (!r.ok) return r
    try {
      await fs.mkdir(r.real, { recursive: true })
      return { ok: true }
    } catch (e) {
      return fsFail(reasonOf(e))
    }
  })

  // Delete a file or directory (recursive). Renderer confirms first.
  ipcMain.handle('fs:delete', async (_e, target: unknown) => {
    if (typeof target !== 'string') return fsFail('io')
    const r = await resolveInsideWorkspace(target)
    if (!r.ok) return r
    try {
      const st = await fs.stat(r.real)
      if (st.isDirectory()) await fs.rm(r.real, { recursive: true })
      else await fs.unlink(r.real)
      return { ok: true }
    } catch (e) {
      return fsFail(reasonOf(e))
    }
  })

  // Rename: change *path*'s name to *newName* (same directory).
  ipcMain.handle('fs:rename', async (_e, path: unknown, newName: unknown) => {
    if (typeof path !== 'string' || typeof newName !== 'string' || !newName.trim()) return fsFail('io')
    const a = await resolveInsideWorkspace(path)
    if (!a.ok) return a
    const b = await resolveInsideWorkspace(join(dirname(path), newName.trim()))
    if (!b.ok) return b
    try {
      await fs.rename(a.real, b.real)
      return { ok: true }
    } catch (e) {
      return fsFail(reasonOf(e))
    }
  })
}

// The main window is created HIDDEN on launch and only shown once xihe serve
// is ready (status 'running') — so the user never waits on a "xihe启动中" UI.
// A safety timeout (SHOW_FORCED_AFTER_MS) guarantees we never trap them on a
// blank launch if serve is slow or crash-looping: better to surface the window
// with the bad status than leave nothing on screen.
let mainWin: BrowserWindow | null = null
let mainShown = false
const SHOW_FORCED_AFTER_MS = 12_000

/** Reveal the hidden main window once. Idempotent — called from the serve
 *  'running'/'errored' status push and the safety timeout alike. */
function maybeShowMain(): void {
  if (mainShown || !mainWin || mainWin.isDestroyed()) return
  // Maximize while still hidden so the first frame already has the final
  // bounds (maximizing after show flashes the 1280x820 default first).
  mainWin.maximize()
  mainWin.show()
  mainShown = true
}

function createWindow(): void {
  mainWin = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 600,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#0a0a0a' : '#f5f6f8',
    title: 'xihe desktop',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true
    }
  })
  mainWin.on('closed', () => { mainWin = null; mainShown = false })

  // NOTE: no ready-to-show → show(). The window stays hidden until serve is
  // ready (maybeShowMain).
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  // electron-vite: dev server URL in dev, built file in production.
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    void mainWin.loadURL(devUrl)
  } else {
    void mainWin.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(async () => {
  // Apply persisted theme BEFORE the first window so the splash background and
  // the renderer's prefers-color-scheme are right on the first frame.
  const settings = await readSettings()
  currentTheme = settings.theme
  nativeTheme.themeSource = currentTheme
  // system mode: an OS theme flip changes the resolved appearance — re-push so
  // the Chrome (dark holds --force-dark-mode; light follows the OS) matches.
  nativeTheme.on('updated', () => {
    if (currentTheme === 'system') pushAppearance()
  })

  registerIpc()
  createWindow() // hidden — revealed by maybeShowMain when serve is ready

  browserPanel = new BrowserPanelController({
    getBaseUrl: () => supervisor?.baseUrl() ?? 'http://127.0.0.1:7788',
    getWin: () => mainWin,
  })

  // serve spawns with no --config, so xihe resolves ~/.xihe-agent/config.yaml
  // (single source — no env injection; the panel edits that file directly via
  // the xiheConfig:save IPC). onStatus pushes state to the renderer AND reveals
  // the main window once serve is ready. start() is fire-and-forget.
  let lastServeState: XiheStatusState | null = null
  supervisor = new ServeSupervisor({
    onStatus: (s: XiheStatus) => {
      const wc = mainWin?.webContents
      if (wc && !wc.isDestroyed()) wc.send('xihe:status', s)
      // Reveal the window once serve is usable (or definitively broken —
      // including not_found, which never transitions to running) so what the
      // user sees is already ready, never a "starting" wait.
      if (s.state === 'running' || s.state === 'errored' || s.state === 'not_found') {
        maybeShowMain()
      }
      // A fresh serve has no snap state — re-snap the browser panel once it's
      // up (only on the transition; liveness re-emits running only on change).
      if (s.state === 'running' && lastServeState !== 'running') {
        browserPanel?.onServeRunning()
        // A fresh serve process lost the in-memory appearance too — re-push so
        // gateway-triggered Chrome launches keep matching the app's theme.
        pushAppearance()
      }
      lastServeState = s.state
    },
  })
  supervisor.start()

  // Safety net: if serve hasn't reached 'running' by now (slow boot, or
  // crash-loop flapping), show the window anyway so the status is visible.
  setTimeout(() => maybeShowMain(), SHOW_FORCED_AFTER_MS)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
      // Dock re-open after close: the user explicitly asked for the window, so
      // show it even if serve happens to be mid-(re)boot.
      maybeShowMain()
    }
  })
})

// Tree-kill the OWNED serve child on quit (adopted/dev serves are left alone).
// No preventDefault — we want the app to actually quit. stop() is idempotent.
app.on('before-quit', () => {
  browserPanel?.release()
  supervisor?.stop()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
