// Typed seam for the `desktop` bridge exposed by the preload
// (src/preload/index.ts). The renderer imports `desktop` from here rather than
// touching `window.desktop` directly, so every IPC shape is typed in one place.
// The renderer is a browser bundle — it cannot import node's `fs`/`path`, which
// is why every desktop-only op funnels through these invoke bridges.

import type { Workspace } from '../store'

/** One immediate child of a directory, as returned by `listDir`. `path` is the
 *  absolute child path, joined by main — the renderer never builds paths itself
 *  (avoids OS-separator bugs on Windows). */
export interface DirEntry {
  name: string
  dir: boolean
  path: string
}

/** readFile result: either readable text (possibly truncated) or a typed
 *  failure the viewer maps to a neutral message. */
export type ReadResult =
  | { ok: true; content: string; size: number; truncated: boolean }
  | { ok: false; reason: 'notFound' | 'notFile' | 'binary' | 'invalid'; size?: number }

/** Result of a destructive fs op (write/create/delete/rename). `reason` is
 *  mapped to a human message at the call site; `outsideWorkspace` means the
 *  main-process sandbox rejected the path (shouldn't happen via normal UI). */
export type FsResult =
  | { ok: true }
  | { ok: false; reason: 'outsideWorkspace' | 'exists' | 'notFound' | 'io' }

/** Process-level status of the built-in xihe serve, pushed by main via
 *  `xihe:status` and pulled on mount. Kept SEPARATE from the WS
 *  `serveConnected` streaming truth: main (this) decides WHEN to try; the WS
 *  client decides whether a streaming connection SUCCEEDED. */
export type XiheStatusState = 'starting' | 'running' | 'stopped' | 'errored' | 'not_found'
export interface XiheStatus {
  /** main-process liveness of the serve child. */
  state: XiheStatusState
  /** true = main spawned it (tree-killed on quit); false = adopted an
   *  external/dev serve (left alone on quit). */
  owned: boolean
  /** /health version once running. */
  version: string | null
  host: string
  port: number
  /** child pid when owned (debug aid). */
  pid?: number | null
  /** human detail, esp. when errored. */
  message?: string
}

/** One window's persisted view state — what the window had open when the app
 *  last closed. Today there is exactly one (the main window); future extra
 *  windows each carry their own record keyed by `id`, with no format change. */
export interface WindowState {
  id: string
  /** Workspace whose scoped sidebar view was open; null = agent-centric. */
  workspaceId: string | null
  /** Conversation open in the chat panel. */
  convId: string | null
  /** Open workbench file tabs per workspace ('' bucket = unbound view) —
   *  editor tabs are workspace-scoped, so each workspace remembers its own
   *  set across restarts. Paths only; diff tabs are session-ephemeral
   *  snapshots and are never persisted. */
  tabsByWorkspace: Record<string, { tabs: string[]; active: string | null }>
}

/** On-disk workspace store (~/.xihe-desktop/workspaces.json). Split by
 *  ownership: `workspaces`/`convWorkspace` are app-global ENTITIES (window-
 *  independent), `windows` is per-window VIEW state restored on boot. */
export interface WorkspaceStore {
  workspaces: Workspace[]
  convWorkspace: Record<string, string>
  windows: WindowState[]
}

/** Effective xihe config surfaced to the UI from ~/.xihe-agent/config.yaml
 *  (single source). api_key is NEVER returned as plaintext — only whether one
 *  is set (api_key_set), so the UI can mask it. Absent keys fall back to xihe's
 *  own defaults. Redeclared in main/xiheConfig.ts across the bundle boundary —
 *  keep in sync. */
export interface XiheConfig {
  model?: string
  base_url?: string
  api_key_set?: boolean
  vision_model?: string
  max_iterations?: number
  compression_threshold?: number
  approvals_mode?: string
  redact_enabled?: boolean
  kbs_enabled?: boolean
  specialists_enabled?: boolean
  image_gen_enabled?: boolean
  tts_enabled?: boolean
}

/** Write patch for xihe config.yaml. Presence of `api_key` writes it ("" clears;
 *  omit keeps the existing key). The other fields are optional — only present
 *  ones are patched (comment-preserving line edit). */
export interface XiheConfigPatch {
  model?: string
  base_url?: string
  api_key?: string
  vision_model?: string
  max_iterations?: number
  compression_threshold?: number
  approvals_mode?: string
  redact_enabled?: boolean
  kbs_enabled?: boolean
  specialists_enabled?: boolean
  image_gen_enabled?: boolean
  tts_enabled?: boolean
}

/** Browser-panel placeholder rect in CSS px, relative to the window content
 *  area — as measured by getBoundingClientRect in the renderer. Main does the
 *  DPI → physical-pixel math; the renderer never scales. */
export interface BrowserRect {
  x: number
  y: number
  w: number
  h: number
}

/** On-disk desktop settings (~/.xihe-desktop/settings.json). Theming is a
 *  desktop capability — this never round-trips through xihe config.yaml. */
export type ThemeMode = 'dark' | 'light' | 'system'
/** chat = conversation-centric (default); workbench = editor-centric layout
 *  for developer workflows (file tree left, editor center, chat right). */
export type LayoutMode = 'chat' | 'workbench'
export interface DesktopSettings {
  theme: ThemeMode
  layoutMode: LayoutMode
}

/** One `git status --porcelain=v1` entry; `path` is absolute with forward
 *  slashes (renderer normalizes tree paths the same way to compare). */
export interface GitStatusEntry {
  path: string
  x: string
  y: string
}
export type GitStatus =
  | { ok: true; repoRoot: string; branch: string; entries: GitStatusEntry[] }
  | { ok: false; reason: 'not-repo' | 'no-git' | 'error' }

/** `git show HEAD:<path>` — the committed base for the editor's gutter change
 *  marks. content null = untracked / no commits yet (whole file is new). */
export type GitHeadFile =
  | { ok: true; content: string | null }
  | { ok: false; reason: 'not-repo' | 'no-git' | 'too-large' | 'error' }

export interface DesktopAPI {
  version: string
  ping: () => Promise<string>
  /** 系统通知（主进程 Notification + AppUserModelId——渲染层 web Notification
   *  在 Windows 开发/未打包构建下会静默失败）。false = 系统不支持。 */
  notify: (title: string, body: string) => Promise<boolean>
  /** Native folder picker; null if the user cancels. */
  openDirectory: () => Promise<string | null>
  /** Immediate children of a directory (zero-stat). Empty on any error. */
  listDir: (path: string) => Promise<DirEntry[]>
  /** Read a file with size cap + binary guard. */
  readFile: (path: string) => Promise<ReadResult>
  /** Create/overwrite a text file at an absolute path (atomic; sandboxed). */
  writeFile: (path: string, content: string) => Promise<FsResult>
  /** Create an empty file *name* inside *parentDir* (sandboxed). */
  createFile: (parentDir: string, name: string) => Promise<FsResult>
  /** Create a directory *name* inside *parentDir*, recursively (sandboxed). */
  createDir: (parentDir: string, name: string) => Promise<FsResult>
  /** Delete a file or directory — recursive if a dir (sandboxed). */
  deletePath: (path: string) => Promise<FsResult>
  /** Rename *path* to *newName* in the same directory (sandboxed). */
  renamePath: (path: string, newName: string) => Promise<FsResult>
  /** Load the workspace store file (empty on missing/corrupt). */
  workspaceLoad: () => Promise<WorkspaceStore>
  /** Persist the workspace store (atomic write). */
  workspaceSave: (data: WorkspaceStore) => Promise<boolean>
  /** Read effective xihe config from ~/.xihe-agent/config.yaml (file value or
   *  xihe's default). api_key returned only as api_key_set, never plaintext. */
  xiheConfigLoad: () => Promise<XiheConfig>
  /** Line-patch xihe config.yaml (comment-preserving). Returns success. */
  xiheConfigSave: (data: XiheConfigPatch) => Promise<boolean>
  /** Restart the serve child so config.yaml edits take effect. */
  serveRestart: () => Promise<boolean>
  /** Open ~/.xihe-desktop/serve.log with the OS default app. */
  openServeLog: () => Promise<boolean>
  /** Load desktop-local settings (theme). */
  settingsLoad: () => Promise<DesktopSettings>
  /** Apply a theme now: nativeTheme + persist + push appearance to xihe.
   *  Returns ok:false on an invalid mode value. */
  setTheme: (theme: ThemeMode) => Promise<{ ok: boolean; theme?: ThemeMode; persisted?: boolean }>
  /** Switch + persist the layout mode (chat vs workbench). Persist-only. */
  setLayoutMode: (
    mode: LayoutMode
  ) => Promise<{ ok: boolean; layoutMode?: LayoutMode; persisted?: boolean }>
  /** `git status` for the repo containing *workdir* (file-tree decorations). */
  gitStatus: (workdir: string) => Promise<GitStatus>
  /** HEAD text of one file (editor change-mark base). */
  gitHeadFile: (path: string) => Promise<GitHeadFile>
  /** Start a one-shot local run (one active run per workdir; supersedes that
   *  workdir's previous run). Output/exit arrive via onRunEvent. */
  runStart: (command: string, cwd: string) => Promise<{ ok: boolean; id?: number }>
  /** Tree-kill the workdir's active run. */
  runStop: (cwd: string) => Promise<boolean>
  /** Persisted per-workdir command history (most recent first). */
  runHistory: () => Promise<Record<string, string[]>>
  onRunEvent: (cb: (ev: RunEvent) => void) => () => void
  /** Attach the shared local shell (created on first call; cwd seeds it).
   *  Returns the tail backlog; further output via onLocalPtyEvent. */
  localPtyAttach: (
    cwd: string | undefined,
    cols: number,
    rows: number
  ) => Promise<{ ok: true; backlog: string } | { ok: false; reason: string }>
  localPtyInput: (data: string) => Promise<boolean>
  localPtyResize: (cols: number, rows: number) => Promise<boolean>
  /** Stop streaming (shell keeps running in main). */
  localPtyDetach: () => Promise<boolean>
  /** Kill the shell (reconnect path — next attach spawns a fresh one). */
  localPtyKill: () => Promise<boolean>
  onLocalPtyEvent: (cb: (ev: LocalPtyEvent) => void) => () => void
  /** Report the browser-panel placeholder rect (CSS px) to main, or null to
   *  un-track: mode 'float' (PinOff) releases the Chrome visible beside the
   *  app, mode 'hide' (panel close) puts it away until the panel returns. */
  setBrowserSnapRegion: (
    rect: BrowserRect | null,
    mode?: 'float' | 'hide'
  ) => Promise<boolean>
  /** Tell main whether the chat layout (the panel's home) is the visible tab —
   *  main hides/re-snaps the Chrome accordingly and gates window-event snaps. */
  setBrowserPanelActive: (active: boolean) => Promise<boolean>
  /** Subscribe to xihe process-status pushes from main; returns unsubscribe. */
  onXiheStatus: (cb: (s: XiheStatus) => void) => () => void
  /** Current xihe process-status snapshot (pulled on mount). */
  getXiheStatus: () => Promise<XiheStatus | null>
}

/** Run-panel event pushed by main (one active run at a time). */
export type RunEvent =
  | { t: 'start'; id: number; command: string; cwd: string }
  | { t: 'out'; id: number; stream: 'out' | 'err'; chunk: string }
  | { t: 'exit'; id: number; code: number | null }

/** Local-terminal event: pty output chunks / shell exit. */
export type LocalPtyEvent = { t: 'out'; chunk: string } | { t: 'exit'; reason: string }

/** Every component imports this, never `window.desktop`. */
export const desktop: DesktopAPI = window.desktop

declare global {
  interface Window {
    desktop: DesktopAPI
  }
}
