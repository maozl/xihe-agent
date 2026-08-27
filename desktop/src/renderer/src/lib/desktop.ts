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

/** On-disk workspace store (~/.xihe-desktop/workspaces.json): the reusable
 *  workspace entities plus the conv→workspace binding map. */
export interface WorkspaceStore {
  workspaces: Workspace[]
  convWorkspace: Record<string, string>
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
export interface DesktopSettings {
  theme: ThemeMode
}

export interface DesktopAPI {
  version: string
  ping: () => Promise<string>
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

/** Every component imports this, never `window.desktop`. */
export const desktop: DesktopAPI = window.desktop

declare global {
  interface Window {
    desktop: DesktopAPI
  }
}
