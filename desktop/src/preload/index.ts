import { contextBridge, ipcRenderer } from 'electron'

// Surface exposed to the renderer under `window.desktop`. The renderer cannot
// import node's `fs`/`path` (contextIsolation + browser bundle), so every
// desktop-only op goes through one of these invoke bridges. Types live in
// renderer/src/lib/desktop.ts (the preload is not imported by the renderer).
const desktop = {
  version: '0.0.1',
  ping: (): Promise<string> => ipcRenderer.invoke('desktop:ping'),
  notify: (title: string, body: string): Promise<boolean> =>
    ipcRenderer.invoke('desktop:notify', title, body),
  openDirectory: (): Promise<string | null> => ipcRenderer.invoke('dialog:openDirectory'),
  listDir: (path: string) => ipcRenderer.invoke('fs:listDir', path),
  readFile: (path: string) => ipcRenderer.invoke('fs:readFile', path),
  writeFile: (path: string, content: string) => ipcRenderer.invoke('fs:writeFile', path, content),
  createFile: (parentDir: string, name: string) => ipcRenderer.invoke('fs:createFile', parentDir, name),
  createDir: (parentDir: string, name: string) => ipcRenderer.invoke('fs:createDir', parentDir, name),
  deletePath: (path: string) => ipcRenderer.invoke('fs:delete', path),
  renamePath: (path: string, newName: string) => ipcRenderer.invoke('fs:rename', path, newName),
  workspaceLoad: () => ipcRenderer.invoke('workspace:load'),
  workspaceSave: (data: unknown) => ipcRenderer.invoke('workspace:save', data),
  // xihe config — main line-patches ~/.xihe-agent/config.yaml (single source).
  // serve reads config at boot, so edits need serveRestart.
  xiheConfigLoad: () => ipcRenderer.invoke('xiheConfig:load'),
  xiheConfigSave: (data: unknown) => ipcRenderer.invoke('xiheConfig:save', data),
  serveRestart: () => ipcRenderer.invoke('serve:restart'),
  // Open ~/.xihe-desktop/serve.log with the OS default app (spawn-error
  // triage from the not_found status card).
  openServeLog: (): Promise<boolean> => ipcRenderer.invoke('shell:openServeLog'),
  // Desktop-local settings + theme. setTheme is click-to-apply: main flips
  // nativeTheme, persists to ~/.xihe-desktop/settings.json, and pushes the
  // resolved light/dark to xihe so the browser panel's Chrome matches.
  settingsLoad: () => ipcRenderer.invoke('settings:load'),
  setTheme: (theme: string) => ipcRenderer.invoke('app:setTheme', theme),
  // Layout mode (chat vs workbench) — persist-only, no native side effects.
  setLayoutMode: (mode: string) => ipcRenderer.invoke('app:setLayoutMode', mode),
  // Git status for file-tree decorations (event-driven, never polled).
  gitStatus: (workdir: string) => ipcRenderer.invoke('git:status', workdir),
  // HEAD text of one file — the base for editor gutter change marks.
  gitHeadFile: (path: string) => ipcRenderer.invoke('git:headFile', path),
  // Run panel — one-shot local command exec in the workspace, output streamed
  // back via onRunEvent; history is persisted per workdir in main.
  runStart: (command: string, cwd: string) => ipcRenderer.invoke('run:start', command, cwd),
  runStop: (cwd: string): Promise<boolean> => ipcRenderer.invoke('run:stop', cwd),
  runHistory: () => ipcRenderer.invoke('run:history'),
  onRunEvent: (cb: (ev: unknown) => void): (() => void) => {
    const handler = (_e: unknown, ev: unknown) => cb(ev)
    ipcRenderer.on('run:event', handler)
    return () => ipcRenderer.removeListener('run:event', handler)
  },
  // Local terminal (ConPTY shell owned by main, survives panel close).
  localPtyAttach: (cwd: string | undefined, cols: number, rows: number) =>
    ipcRenderer.invoke('localPty:attach', cwd, cols, rows),
  localPtyInput: (data: string) => ipcRenderer.invoke('localPty:input', data),
  localPtyResize: (cols: number, rows: number) =>
    ipcRenderer.invoke('localPty:resize', cols, rows),
  localPtyDetach: (): Promise<boolean> => ipcRenderer.invoke('localPty:detach'),
  localPtyKill: (): Promise<boolean> => ipcRenderer.invoke('localPty:kill'),
  onLocalPtyEvent: (cb: (ev: unknown) => void): (() => void) => {
    const handler = (_e: unknown, ev: unknown) => cb(ev)
    ipcRenderer.on('localPty:event', handler)
    return () => ipcRenderer.removeListener('localPty:event', handler)
  },
  // Browser panel — the placeholder rect (CSS px, content-area coords) drives
  // the Win32 snap in main + serve; null un-tracks ('float' releases the
  // Chrome beside the app — PinOff; 'hide' puts it away until the panel
  // returns — panel close / tab switch).
  setBrowserSnapRegion: (rect: unknown, mode?: 'float' | 'hide') =>
    ipcRenderer.invoke('browser:setSnapRegion', rect, mode),
  // Chat-layout visibility (tab switch) — main hides/re-snaps the Chrome and
  // gates its window-event snaps on it, so renderer-driven hides can't race
  // a focus-driven snap.
  setBrowserPanelActive: (active: boolean) =>
    ipcRenderer.invoke('browser:setPanelActive', active),
  // Push bridge: subscribe to xihe process-status changes from main. Returns an
  // unsubscribe that removes the SAME handler reference (React StrictMode dev
  // mounts/unmounts/remounts; a stray double-subscription would fire twice).
  onXiheStatus: (cb: (s: unknown) => void): (() => void) => {
    const handler = (_e: unknown, s: unknown) => cb(s)
    ipcRenderer.on('xihe:status', handler)
    return () => ipcRenderer.removeListener('xihe:status', handler)
  },
  // Pull bridge: current xihe status snapshot, fetched on mount to cover the
  // pre-did-finish-load window where an early `running` push can be dropped.
  getXiheStatus: (): Promise<unknown> => ipcRenderer.invoke('xihe:status')
}

contextBridge.exposeInMainWorld('desktop', desktop)
