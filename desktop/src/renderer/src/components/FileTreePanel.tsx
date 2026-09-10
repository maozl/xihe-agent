import { Suspense, lazy, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Check,
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  File as FileIcon,
  FilePlus,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderTree,
  GitBranch,
  PanelLeftClose,
  PanelRightClose,
  Pencil,
  Play,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { guessRunCommand } from './EditorArea'
import { desktop, type DirEntry, type FsResult, type GitStatus } from '../lib/desktop'
import { detectLanguage } from '../lib/lang'
import { useStore, type Workspace } from '../store'
import { cn } from '../lib/cn'
import { Resizer, usePanelSize } from './Resizer'

/** The failure half of {@link FsResult} — the reason strings main can return. */
type FsFailReason = Exclude<FsResult, { ok: true }>['reason']

// Monaco is heavy — only the drawer that actually renders it pulls the chunk.
const MonacoPane = lazy(() =>
  import('./monaco/MonacoPane').then((m) => ({ default: m.MonacoPane }))
)

interface Props {
  workspace: Workspace
  className?: string
  /** Collapses the right-hand panel to an edge strip (owned by App). */
  onCollapse?: () => void
  /** Run-panel prefill (Play hover action on runnable files). */
  onRunFile?: (command: string) => void
}

/** 扁平（单链目录合并）展示 preference — app-wide (both tree instances share
 *  it). 'flat' is the previous value of the same preference (superseded
 *  flat-list UI) — treat it as compact. */
function readCompactMode(): boolean {
  try {
    const v = localStorage.getItem('fileTreeMode')
    return v === 'compact' || v === 'flat'
  } catch {
    return false
  }
}

type Content =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'ok'; text: string; size: number; truncated: boolean; name: string }
  | { state: 'binary'; name: string; size?: number }
  | { state: 'error'; name: string }

// Pure path-string helpers (UI state only — the renderer never builds fs paths;
// real paths come from main via node's `path`. Used for activeDir targeting +
// selection tracking after a rename.)
function parentDir(p: string): string {
  const idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'))
  if (idx <= 0) return p
  const parent = p.slice(0, idx)
  // Windows drive root ('E:') → reattach the separator so it stays absolute.
  if (/^[A-Za-z]:$/.test(parent)) return parent + '\\'
  return parent
}
// The new path a rename would produce (parent + newName), preserving the
// separator style of the original. Lets us keep viewing a file after renaming.
function withNewName(oldPath: string, newName: string): string {
  const parent = parentDir(oldPath)
  const sep = oldPath.includes('\\') ? '\\' : '/'
  return parent.endsWith(sep) ? parent + newName : parent + sep + newName
}
const baseName = (p: string): string => p.split(/[\\/]/).filter(Boolean).pop() ?? p

// Separator/case-insensitive path key — tree paths come from node's path.join
// (backslashes on Windows), git paths are forward-slash absolute.
const normKey = (p: string): string => p.replace(/\\/g, '/').toLowerCase()
const parentPosix = (p: string): string => {
  const i = p.lastIndexOf('/')
  return i <= 0 ? p : p.slice(0, i)
}

/** Collapse porcelain XY to the one letter the row badge shows. Deletion is
 *  the most alarming, so it wins ties. */
function gitLabel(x: string, y: string): string {
  if (x === '?' && y === '?') return 'U'
  const s = x + y
  if (s.includes('D')) return 'D'
  if (s.includes('R')) return 'R'
  if (s.includes('C')) return 'C'
  if (s.includes('U')) return 'U'
  if (s.includes('A')) return 'A'
  return 'M'
}

// M/R/C→modified-family, A/U→new-family, D→deleted — reuses the existing
// semantic tokens, no new CSS variables.
const GIT_COLOR: Record<string, string> = {
  M: 'text-warning',
  R: 'text-warning',
  C: 'text-warning',
  A: 'text-success',
  U: 'text-success',
  D: 'text-danger',
}

function reasonText(reason: FsFailReason): string {
  switch (reason) {
    case 'outsideWorkspace':
      return '不在工作空间内'
    case 'exists':
      return '已存在同名项'
    case 'notFound':
      return '不存在'
    case 'io':
    default:
      return '读写错误'
  }
}

interface FileTreeProps {
  workspace: Workspace
  className?: string
  /** Path to highlight as the open file (viewer selection / editor tab). */
  selectedPath?: string | null
  /** File click — chat mode opens the drawer viewer, workbench opens a tab. */
  onSelectFile: (entry: DirEntry) => void
  /** Tree CRUD follow-ups so the viewer / editor tabs can track the change. */
  onPathRenamed?: (oldPath: string, newPath: string) => void
  onPathDeleted?: (path: string) => void
  /** Run-panel prefill for runnable files (Play hover action) — receives the
   *  guessed command, not the path. */
  onRunFile?: (command: string) => void
  /** Which layout edge this tree sits on — picks the collapse chevron's
   *  direction. */
  side?: 'left' | 'right'
  /** Rendered only when the embedding layout supports collapsing to a strip. */
  onCollapse?: () => void
  /** Slot below the tree (chat mode mounts its viewer drawer here; workbench
   *  uses the tree bare). */
  children?: ReactNode
}

/** Workspace file tree with inline CRUD. Pure-desktop: all fs access goes
 *  through the `desktop` IPC bridge (never serve). Destructive writes are
 *  sandboxed to a workspace root in main; this UI only ever asks for paths
 *  the sandbox allows. Directories load lazily on expand; a file click is
 *  delegated to the embedding layout via onSelectFile. */
export function FileTree({
  workspace,
  className,
  selectedPath,
  onSelectFile,
  onPathRenamed,
  onPathDeleted,
  onRunFile,
  side = 'left',
  onCollapse,
  children,
}: FileTreeProps) {
  // path → cached children (null = not yet loaded). Root key = workspace.workdir.
  const [tree, setTree] = useState<Record<string, DirEntry[] | null>>({})
  // Column width (per side — workbench left vs chat right have different
  // defaults) with its drag handle on the panel's inner edge.
  const [width, setWidth] = usePanelSize(
    `fileTreeWidth:${side}`,
    side === 'left' ? 240 : 320,
    180
  )
  const asideRef = useRef<HTMLElement>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // IDEA-style compact display: a directory whose only child is a directory
  // merges into one node (src/main/java → one row). Toggle in the header;
  // preference is app-wide.
  const [compactMode, setCompactMode] = useState(readCompactMode)
  // Target directory for "new file/folder": the folder last opened, the parent
  // of the file last opened, or the workspace root. Shown next to the buttons.
  const [activeDir, setActiveDir] = useState<string>(workspace.workdir)

  // Inline-create input: which parent dir, and file vs folder.
  const [creating, setCreating] = useState<{ parent: string; kind: 'file' | 'dir' } | null>(null)
  const [createValue, setCreateValue] = useState('')
  // Inline-rename input: which entry is being renamed.
  const [renaming, setRenaming] = useState<{ path: string; dir: boolean; name: string } | null>(null)
  const [renameValue, setRenameValue] = useState('')
  // One transient message for whichever inline form last failed (shown in place).
  const [formError, setFormError] = useState<string | null>(null)
  const fsVersion = useStore((s) => s.fsVersion)
  const [git, setGit] = useState<GitStatus | null>(null)

  async function loadGit(): Promise<void> {
    setGit(await desktop.gitStatus(workspace.workdir))
  }

  // Chain walks trigger child loads during render — dedupe in-flight fetches.
  const pendingLoads = useRef(new Set<string>())

  /** Fetch a dir's children into the tree cache (no-op when loaded/loading). */
  function ensureChildren(path: string): void {
    if (tree[path] != null || pendingLoads.current.has(path)) return
    pendingLoads.current.add(path)
    void desktop.listDir(path).then((es) => {
      pendingLoads.current.delete(path)
      setTree((t) => ({ ...t, [path]: es ?? [] }))
    })
  }

  /** IDEA-style compaction: starting at *entry*, merge the chain of
   *  single-directory children into one node (src/main/java → `src.main.java`).
   *  Stops at the first branch / file / not-yet-loaded dir — ensureChildren
   *  makes the merge continue on the next render once that load lands. The
   *  iteration cap is a symlink-cycle backstop (a dir symlinked to its own
   *  parent would otherwise walk forever). Badge aggregates the chain: a
   *  deleted file badges a middle dir that no longer shows it on disk. */
  function chainOf(entry: DirEntry): { path: string; label: string; badge?: string } {
    let path = entry.path
    let label = entry.name
    let badge = gitIndex.byDir.get(normKey(path))
    for (let i = 0; i < 50; i++) {
      const children = tree[path]
      if (children == null) {
        ensureChildren(path)
        break
      }
      if (children.length !== 1 || !children[0].dir) break
      path = children[0].path
      label += '.' + children[0].name
      badge = badge ?? gitIndex.byDir.get(normKey(path))
    }
    return { path, label, badge }
  }

  function toggleCompactMode(): void {
    const next = !compactMode
    setCompactMode(next)
    try {
      localStorage.setItem('fileTreeMode', next ? 'compact' : 'tree')
    } catch {
      /* storage unavailable — session-only preference */
    }
  }

  // Load the root whenever the workspace changes; `cancelled` guards the
  // StrictMode double-invoke in dev. Also resets every per-workspace state.
  useEffect(() => {
    let cancelled = false
    setExpanded({})
    setTree({})
    setActiveDir(workspace.workdir)
    setCreating(null)
    setRenaming(null)
    setFormError(null)
    setGit(null)
    void desktop.listDir(workspace.workdir).then((entries) => {
      if (!cancelled) setTree({ [workspace.workdir]: entries ?? [] })
    })
    void loadGit()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.workdir])

  // Disk moved under the tree (agent writes / CRUD / editor saves) — re-read
  // the tree + git status after a short debounce (no fs.watch in MVP). The
  // closure snapshots expanded at bump time; a folder opened inside the window
  // catches the NEXT bump or a manual refresh.
  useEffect(() => {
    if (fsVersion === 0) return
    const t = setTimeout(() => {
      void refresh()
      void loadGit()
    }, 500)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsVersion, workspace.workdir])

  // Row-decoration lookup built from git status: files by their own path,
  // directories by the first change found under them (any ancestor of a
  // changed file carries a dimmed badge).
  const gitIndex = useMemo(() => {
    const byFile = new Map<string, string>()
    const byDir = new Map<string, string>()
    if (!git?.ok) return { byFile, byDir }
    const rootKey = normKey(git.repoRoot)
    for (const e of git.entries) {
      const label = gitLabel(e.x, e.y)
      byFile.set(normKey(e.path), label)
      for (let dir = parentPosix(e.path); ; dir = parentPosix(dir)) {
        const k = normKey(dir)
        if (k === rootKey) break
        if (!byDir.has(k)) byDir.set(k, label)
        const parent = parentPosix(dir)
        if (parent === dir) break
      }
    }
    return { byFile, byDir }
  }, [git])

  async function toggle(entry: DirEntry): Promise<void> {
    if (!entry.dir) return
    const isOpen = !!expanded[entry.path]
    setExpanded((e) => ({ ...e, [entry.path]: !isOpen }))
    // Opening a folder makes it the active target for "new …".
    if (!isOpen) setActiveDir(entry.path)
    if (!isOpen && tree[entry.path] == null) {
      const entries = (await desktop.listDir(entry.path)) ?? []
      setTree((t) => ({ ...t, [entry.path]: entries }))
    }
  }

  // Re-fetch whatever the active mode shows. Runs on manual refresh and on
  // fsVersion bumps (agent writes / desktop-side CRUD) — no fs.watch in MVP.
  // Git status rides along so the manual button refreshes decorations too.
  async function refresh(): Promise<void> {
    const paths = [workspace.workdir, ...Object.keys(expanded)]
    const lists = await Promise.all(paths.map((p) => desktop.listDir(p)))
    const updates: Record<string, DirEntry[]> = {}
    paths.forEach((p, i) => {
      updates[p] = lists[i]
    })
    setTree((t) => ({ ...t, ...updates }))
    void loadGit()
  }

  async function confirmCreate(): Promise<void> {
    if (!creating) return
    const name = createValue.trim()
    if (!name) {
      setCreating(null)
      setFormError(null)
      return
    }
    const r =
      creating.kind === 'dir'
        ? await desktop.createDir(creating.parent, name)
        : await desktop.createFile(creating.parent, name)
    if (!r.ok) {
      setFormError(reasonText(r.reason))
      return
    }
    setFormError(null)
    const parent = creating.parent
    const newPath = childPath(parent, name)
    setCreating(null)
    setCreateValue('')
    useStore.getState().bumpFsVersion()
    await refresh()
    // For a new file, hand it to the embedding layout (viewer / new tab); for
    // a folder, expand it so the user sees (and can populate) the empty dir.
    if (creating.kind === 'file') {
      onSelectFile({ name, dir: false, path: newPath })
    } else {
      setExpanded((e) => ({ ...e, [newPath]: true }))
    }
  }

  // Build the absolute child path on the renderer side only to drive
  // selection/open after a create — main is what actually wrote it.
  function childPath(parent: string, name: string): string {
    const sep = parent.includes('\\') ? '\\' : '/'
    return parent.endsWith(sep) ? parent + name : parent + sep + name
  }

  function cancelCreate(): void {
    setCreating(null)
    setCreateValue('')
    setFormError(null)
  }

  async function confirmRename(): Promise<void> {
    if (!renaming) return
    const newName = renameValue.trim()
    if (!newName || newName === renaming.name) {
      setRenaming(null)
      setFormError(null)
      return
    }
    const r = await desktop.renamePath(renaming.path, newName)
    if (!r.ok) {
      setFormError(reasonText(r.reason))
      return
    }
    setFormError(null)
    const oldPath = renaming.path
    const newPath = withNewName(oldPath, newName)
    setRenaming(null)
    setRenameValue('')
    onPathRenamed?.(oldPath, newPath)
    useStore.getState().bumpFsVersion()
    await refresh()
  }

  function cancelRename(): void {
    setRenaming(null)
    setRenameValue('')
    setFormError(null)
  }

  async function onDelete(entry: DirEntry): Promise<void> {
    const msg = entry.dir
      ? `删除「${entry.name}」及其所有内容？此操作不可撤销。`
      : `删除「${entry.name}」？此操作不可撤销。`
    if (!window.confirm(msg)) return
    setFormError(null)
    const r = await desktop.deletePath(entry.path)
    if (!r.ok) {
      setFormError(reasonText(r.reason))
      return
    }
    onPathDeleted?.(entry.path)
    useStore.getState().bumpFsVersion()
    await refresh()
  }

  function renderEntries(
    entries: DirEntry[] | null | undefined,
    depth: number,
    dirPath: string
  ): ReactNode {
    if (entries == null && !(creating && creating.parent === dirPath)) return null
    const rows: ReactNode[] = []
    // Inline "new …" input at the top of the active target dir's children.
    if (creating && creating.parent === dirPath) {
      rows.push(
        <NameEditRow
          key="__create"
          padLeft={8 + (depth + 1) * 12}
          icon={
            creating.kind === 'dir' ? (
              <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
            ) : (
              <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
            )
          }
          value={createValue}
          placeholder={creating.kind === 'dir' ? '文件夹名' : '文件名'}
          onChange={setCreateValue}
          onConfirm={() => void confirmCreate()}
          onCancel={cancelCreate}
        />
      )
    }
    if (entries) {
      for (const entry of entries) {
        // Compact mode: a dir merges with its chain of single-dir children
        // into one node (path = chain end, label = dot-joined names).
        const chain = compactMode && entry.dir ? chainOf(entry) : null
        const nodePath = chain?.path ?? entry.path
        const nodeLabel = chain?.label ?? entry.name
        const isOpen = !!expanded[nodePath]
        const children = tree[nodePath]
        const isRenaming = renaming?.path === nodePath
        const runCmd = !entry.dir ? guessRunCommand(entry.path) : null
        const gitBadge =
          chain?.badge ??
          gitIndex.byFile.get(normKey(entry.path)) ??
          (entry.dir ? gitIndex.byDir.get(normKey(nodePath)) : undefined)
        rows.push(
          <div key={entry.path} className="group relative">
            {isRenaming ? (
              <NameEditRow
                padLeft={8 + depth * 12}
                lead={
                  entry.dir ? (
                    <ChevronRight className="h-3 w-3 shrink-0 text-ink-4" />
                  ) : undefined
                }
                icon={
                  entry.dir ? (
                    <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
                  ) : (
                    <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
                  )
                }
                value={renameValue}
                onChange={setRenameValue}
                onConfirm={() => void confirmRename()}
                onCancel={cancelRename}
              />
            ) : (
              <>
                <button
                  onClick={() =>
                    entry.dir
                      ? void toggle({ name: nodeLabel, dir: true, path: nodePath })
                      : onSelectFile(entry)
                  }
                  style={{ paddingLeft: 8 + depth * 12 }}
                  title={nodePath}
                  className={cn(
                    'flex w-full items-center gap-1 py-1 pr-16 text-left text-xs transition hover:bg-elevated/60',
                    selectedPath === entry.path && 'bg-elevated text-ink'
                  )}
                >
                  {entry.dir ? (
                    <>
                      {isOpen ? (
                        <ChevronDown className="h-3 w-3 shrink-0 text-ink-4" />
                      ) : (
                        <ChevronRight className="h-3 w-3 shrink-0 text-ink-4" />
                      )}
                      {isOpen ? (
                        <FolderOpen className="h-3.5 w-3.5 shrink-0 text-accent" />
                      ) : (
                        <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
                      )}
                    </>
                  ) : (
                    <>
                      <span className="w-3 shrink-0" />
                      <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
                    </>
                  )}
                  <span className="truncate">{nodeLabel}</span>
                  {gitBadge && (
                    <span
                      className={cn(
                        'ml-1 shrink-0 font-mono text-[9px]',
                        GIT_COLOR[gitBadge] ?? 'text-ink-4',
                        // Directory badge is aggregated — dim it vs the file's own.
                        entry.dir && 'opacity-60'
                      )}
                    >
                      {gitBadge}
                    </span>
                  )}
                </button>
                {/* Hover actions (per row, file or folder). pr-16 on the row
                 * above keeps the name clear of these. A compact node deletes
                 * from the chain TOP (recursive — covers the whole node) and
                 * renames the chain END segment. */}
                <RowActions
                  runCmd={runCmd}
                  onRunFile={onRunFile}
                  onRename={() => {
                    setFormError(null)
                    const name = chain ? baseName(nodePath) : entry.name
                    setRenaming({ path: nodePath, dir: entry.dir, name })
                    setRenameValue(name)
                  }}
                  onDelete={() => void onDelete(entry)}
                />
              </>
            )}
            {entry.dir && isOpen && renderEntries(children, depth + 1, nodePath)}
          </div>
        )
      }
    }
    return rows
  }

  const rootEntries = tree[workspace.workdir]

  return (
    <aside
      ref={asideRef}
      className={cn('relative flex flex-col bg-panel/40', className)}
      style={{ width }}
    >
      <Resizer
        axis="col"
        title="拖动调整面板宽度"
        onMove={(x) => {
          const el = asideRef.current
          if (!el) return
          const rect = el.getBoundingClientRect()
          const w = side === 'left' ? x - rect.left : rect.right - x
          const max = Math.max(window.innerWidth - 420, 240)
          setWidth(Math.min(Math.max(w, 180), max))
        }}
        className={cn(
          'absolute top-0 h-full w-1.5',
          side === 'left' ? 'right-0 translate-x-1/2' : 'left-0 -translate-x-1/2'
        )}
      />
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="flex-1 truncate text-xs font-medium" title={workspace.workdir}>
          {workspace.name}
        </span>
        <button
          onClick={toggleCompactMode}
          title={
            compactMode
              ? '恢复逐层目录'
              : '扁平化单链目录（IDEA 风格：单链目录合并为一个节点，如 src.main.java）'
          }
          className={cn(
            'rounded p-1 transition',
            compactMode
              ? 'bg-contrast text-sky-300'
              : 'text-ink-4 hover:bg-elevated hover:text-ink'
          )}
        >
          {compactMode ? (
            <ChevronsDownUp className="h-3.5 w-3.5" />
          ) : (
            <FolderTree className="h-3.5 w-3.5" />
          )}
        </button>
        <button
          onClick={() => {
            setFormError(null)
            setCreating({ parent: activeDir, kind: 'file' })
            setCreateValue('')
            // Make sure the target dir is open so the inline input is visible.
            setExpanded((e) => ({ ...e, [activeDir]: true }))
            ensureChildren(activeDir)
          }}
          title={`在「${baseName(activeDir)}」下新建文件`}
          className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink"
        >
          <FilePlus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => {
            setFormError(null)
            setCreating({ parent: activeDir, kind: 'dir' })
            setCreateValue('')
            setExpanded((e) => ({ ...e, [activeDir]: true }))
            ensureChildren(activeDir)
          }}
          title={`在「${baseName(activeDir)}」下新建文件夹`}
          className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink"
        >
          <FolderPlus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => void refresh()}
          title="刷新"
          className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
        >
          <RefreshCw className="h-3 w-3" />
        </button>
        {onCollapse && (
          <button
            onClick={onCollapse}
            title="折叠文件树"
            className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
          >
            {side === 'right' ? (
              <PanelRightClose className="h-3.5 w-3.5" />
            ) : (
              <PanelLeftClose className="h-3.5 w-3.5" />
            )}
          </button>
        )}
      </div>

      {git?.ok && (
        <div className="flex items-center gap-1.5 border-b border-line px-3 py-1 text-[10px] text-ink-4">
          <GitBranch className="h-3 w-3 shrink-0" />
          <span className="truncate" title={git.repoRoot}>
            {git.branch || '(无分支)'}
          </span>
          <span className="ml-auto shrink-0 tabular-nums">{git.entries.length} 处变更</span>
        </div>
      )}
      {/* not-repo / no-git stay fully silent (no version control is a normal
       *  state); only a FAILED status run is worth a hint — the decorations
       *  are missing for a reason the user can act on. */}
      {git && !git.ok && git.reason === 'error' && (
        <div
          className="flex items-center gap-1.5 border-b border-line px-3 py-1 text-[10px] text-ink-4"
          title="git status 执行失败（仓库损坏或 git 拒绝访问，如 dubious ownership）"
        >
          <GitBranch className="h-3 w-3 shrink-0" />
          git 状态不可用
        </div>
      )}

      {formError && (
        <div className="border-b border-danger/40 bg-danger/10 px-3 py-1 text-[11px] text-danger">
          {formError}
        </div>
      )}

      <div className="flex-1 overflow-auto py-1">
        {rootEntries == null && !(creating && creating.parent === workspace.workdir) ? (
          <div className="px-3 py-2 text-xs text-ink-4">加载中…</div>
        ) : rootEntries?.length === 0 &&
          !(creating && creating.parent === workspace.workdir) ? (
          <div className="px-3 py-2 text-xs text-ink-4">空目录</div>
        ) : (
          renderEntries(rootEntries, 0, workspace.workdir)
        )}
      </div>
      {children}
    </aside>
  )
}

/** Inline create/rename input shared by the tree renderers: optional chevron
 *  slot, entry icon, text input, confirm/cancel. */
function NameEditRow({ padLeft, lead, icon, value, placeholder, onChange, onConfirm, onCancel }: {
  padLeft: number
  lead?: ReactNode
  icon: ReactNode
  value: string
  placeholder?: string
  onChange: (v: string) => void
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="flex items-center gap-1 py-1 pr-2" style={{ paddingLeft: padLeft }}>
      {lead ?? <span className="w-3 shrink-0" />}
      {icon}
      <input
        autoFocus
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onConfirm()
          } else if (e.key === 'Escape') {
            e.preventDefault()
            onCancel()
          }
        }}
        onBlur={onCancel}
        onFocus={(e) => e.target.select()}
        className="min-w-0 flex-1 rounded border border-sky-700/70 bg-app px-1 py-0.5 text-xs text-ink outline-none"
      />
      <button
        onMouseDown={(e) => e.preventDefault()}
        onClick={onConfirm}
        title="确认"
        className="rounded p-0.5 text-success transition hover:bg-elevated"
      >
        <Check className="h-3 w-3" />
      </button>
      <button
        onMouseDown={(e) => e.preventDefault()}
        onClick={onCancel}
        title="取消"
        className="rounded p-0.5 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  )
}

/** Hover actions for one row: run / rename / delete. Rendered inside a
 *  `group relative` row; pr-16 on the row keeps the name clear. */
function RowActions({ runCmd, onRunFile, onRename, onDelete }: {
  runCmd: string | null
  onRunFile?: (command: string) => void
  onRename: () => void
  onDelete: () => void
}) {
  return (
    <div className="absolute right-1 top-1/2 flex -translate-y-1/2 gap-0.5 opacity-0 transition group-hover:opacity-100">
      {onRunFile && runCmd != null && (
        <button
          onClick={() => onRunFile(runCmd)}
          title="运行（在运行面板中执行，命令可改）"
          className="rounded p-0.5 text-ink-4 transition hover:bg-strong hover:text-success"
        >
          <Play className="h-3 w-3" />
        </button>
      )}
      <button
        onClick={onRename}
        title="重命名"
        className="rounded p-0.5 text-ink-4 transition hover:bg-strong hover:text-ink"
      >
        <Pencil className="h-3 w-3" />
      </button>
      <button
        onClick={onDelete}
        title="删除"
        className="rounded p-0.5 text-ink-4 transition hover:bg-strong hover:text-danger"
      >
        <Trash2 className="h-3 w-3" />
      </button>
    </div>
  )
}

/** Chat-mode file panel: the tree plus a single-file viewer drawer (Monaco,
 *  lazily loaded). In workbench mode the tree is embedded bare and files open
 *  as editor tabs instead — see App.tsx. */
export function FileTreePanel({ workspace, className, onCollapse, onRunFile }: Props) {
  const fsVersion = useStore((s) => s.fsVersion)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [content, setContent] = useState<Content>({ state: 'idle' })

  // Editor draft (only meaningful when content is editable text). Synced from
  // content whenever a new (non-truncated) file loads.
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  // Keep the editor draft in sync with the loaded file: on open and after a
  // successful save (which produces a new content object), reset the editor
  // to the canonical text and clear dirty.
  useEffect(() => {
    if (content.state === 'ok' && !content.truncated) {
      setDraft(content.text)
    }
  }, [content])

  // Reset the viewer when the workspace changes.
  useEffect(() => {
    setSelectedFile(null)
    setContent({ state: 'idle' })
    setDraft('')
    setFormError(null)
  }, [workspace.workdir])

  const editable = content.state === 'ok' && !content.truncated
  const dirty = editable && draft !== content.text

  async function openFile(entry: DirEntry): Promise<void> {
    setSelectedFile(entry.path)
    setContent({ state: 'loading' })
    setFormError(null)
    const r = await desktop.readFile(entry.path)
    if (!r.ok) {
      setContent(
        r.reason === 'binary'
          ? { state: 'binary', name: entry.name, size: r.size }
          : { state: 'error', name: entry.name }
      )
      return
    }
    // Files >1 MB come back as truncated text (main reads the first MiB) rather
    // than a failure — flagged so the viewer stays read-only (see editor guard).
    setContent({
      state: 'ok',
      text: r.content,
      size: r.size,
      truncated: r.truncated,
      name: entry.name,
    })
  }

  // Agent writes / tree CRUD moved the file under the viewer — reload the
  // canonical text unless there are unsaved edits (overwriting a dirty draft
  // silently would eat the user's work).
  useEffect(() => {
    if (fsVersion === 0 || !selectedFile || dirty) return
    let cancelled = false
    void desktop.readFile(selectedFile).then((r) => {
      if (cancelled || !selectedFile) return
      if (!r.ok) {
        setContent({ state: 'error', name: baseName(selectedFile) })
        return
      }
      setContent((c) =>
        c.state === 'ok'
          ? { ...c, text: r.content, size: r.size, truncated: r.truncated }
          : c
      )
    })
    return () => {
      cancelled = true
    }
    // dirty is deliberately read as a snapshot — a bump while dirty keeps the
    // draft (the banner-free compromise of this drawer).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsVersion])

  async function saveDraft(): Promise<void> {
    if (!selectedFile || !editable || saving) return
    setSaving(true)
    const r = await desktop.writeFile(selectedFile, draft)
    setSaving(false)
    if (!r.ok) {
      setFormError(reasonText(r.reason))
      return
    }
    setFormError(null)
    // New content object → the draft-sync effect resets the editor + clears dirty.
    setContent({ ...content, text: draft })
    useStore.getState().bumpFsVersion()
  }

  return (
    <FileTree
      workspace={workspace}
      className={className}
      selectedPath={selectedFile}
      side="right"
      onCollapse={onCollapse}
      onRunFile={onRunFile}
      onSelectFile={(entry) => void openFile(entry)}
      onPathRenamed={(oldPath, newPath) => {
        // If the renamed entry (or anything under it) was the open file,
        // follow it under its new name so the viewer stays in sync.
        if (
          selectedFile &&
          (selectedFile === oldPath ||
            selectedFile.startsWith(oldPath + '\\') ||
            selectedFile.startsWith(oldPath + '/'))
        ) {
          setSelectedFile(newPath + selectedFile.slice(oldPath.length))
        }
      }}
      onPathDeleted={(path) => {
        if (
          selectedFile &&
          (selectedFile === path ||
            selectedFile.startsWith(path + '\\') ||
            selectedFile.startsWith(path + '/'))
        ) {
          setSelectedFile(null)
          setContent({ state: 'idle' })
        }
      }}
    >
      {selectedFile && (
        <div className="flex h-2/5 flex-col border-t border-line">
          <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
            <FileIcon className="h-3 w-3 shrink-0 text-ink-4" />
            <span
              className="flex-1 truncate text-[11px] text-ink-3"
              title={selectedFile}
            >
              {content.state === 'ok' || content.state === 'binary' || content.state === 'error'
                ? content.name
                : baseName(selectedFile)}
            </span>
            {editable && (
              <>
                {dirty && <span className="text-[10px] text-warning/80">未保存</span>}
                <button
                  onClick={() => setDraft(content.state === 'ok' ? content.text : '')}
                  disabled={!dirty || saving}
                  title="放弃修改"
                  className="rounded px-1.5 py-0.5 text-[11px] text-ink-3 transition hover:bg-elevated hover:text-ink disabled:opacity-40"
                >
                  放弃
                </button>
                <button
                  onClick={() => void saveDraft()}
                  disabled={!dirty || saving}
                  title="保存（Ctrl+S）"
                  className="rounded px-1.5 py-0.5 text-[11px] text-success transition hover:bg-elevated disabled:opacity-40"
                >
                  {saving ? '保存中…' : '保存'}
                </button>
              </>
            )}
            <button
              onClick={() => {
                setSelectedFile(null)
                setContent({ state: 'idle' })
              }}
              className="rounded p-0.5 text-ink-4 transition hover:text-ink-2"
              title="关闭"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
          <div className="min-h-0 flex-1">
            {content.state === 'loading' && (
              <div className="px-3 py-2 text-xs text-ink-4">读取中…</div>
            )}
            {content.state === 'ok' &&
              (content.truncated ? (
                <>
                  <pre className="whitespace-pre-wrap break-all p-3 font-mono text-[11px] leading-relaxed text-ink-3">
                    {content.text}
                  </pre>
                  <div className="border-t border-line px-3 py-1.5 text-[10px] text-warning/80">
                    文件过大（{(content.size / 1024 / 1024).toFixed(1)} MB），仅显示前 1 MB。
                    为避免保存时截断未显示内容，此处只读——请用 agent 或外部编辑器修改。
                  </div>
                </>
              ) : (
                <Suspense
                  fallback={<div className="px-3 py-2 text-xs text-ink-4">编辑器加载中…</div>}
                >
                  <MonacoPane
                    path={selectedFile}
                    value={draft}
                    language={detectLanguage(selectedFile)}
                    wordWrap
                    onChange={setDraft}
                    onSave={() => void saveDraft()}
                  />
                </Suspense>
              ))}
            {content.state === 'binary' && (
              <div className="px-3 py-2 text-xs text-ink-4">
                二进制文件
                {content.size ? `（${Math.max(1, Math.round(content.size / 1024))} KB）` : ''}，无法显示
              </div>
            )}
            {content.state === 'error' && (
              <div className="px-3 py-2 text-xs text-ink-4">读取失败</div>
            )}
          </div>
        </div>
      )}
    </FileTree>
  )
}
