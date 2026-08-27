import { useEffect, useState, type ReactNode } from 'react'
import {
  Check,
  ChevronDown,
  ChevronRight,
  File as FileIcon,
  FilePlus,
  Folder,
  FolderOpen,
  FolderPlus,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { desktop, type DirEntry, type FsResult } from '../lib/desktop'

/** The failure half of {@link FsResult} — the reason strings main can return. */
type FsFailReason = Exclude<FsResult, { ok: true }>['reason']
import type { Workspace } from '../store'
import { cn } from '../lib/cn'

interface Props {
  workspace: Workspace
  className?: string
}

// Cap recursion so a pathological tree can't blow the stack / DOM.
const MAX_DEPTH = 8

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

/** Workspace file tree with inline CRUD. Pure-desktop: all fs access goes
 *  through the `desktop` IPC bridge (never serve). Destructive writes are
 *  sandboxed to a workspace root in main; this UI only ever asks for paths the
 *  sandbox allows. Directories load lazily on expand; a file click reads it
 *  into the viewer, which is editable unless the file was truncated (>1 MB) —
 *  editing a truncated read would lose the unread tail on save. */
export function FileTreePanel({ workspace, className }: Props) {
  // path → cached children (null = not yet loaded). Root key = workspace.workdir.
  const [tree, setTree] = useState<Record<string, DirEntry[] | null>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [content, setContent] = useState<Content>({ state: 'idle' })
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

  // Editor draft (only meaningful when content is editable text). Synced from
  // content whenever a new (non-truncated) file loads.
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  // Load the root whenever the workspace changes; `cancelled` guards the
  // StrictMode double-invoke in dev. Also resets every per-workspace state.
  useEffect(() => {
    let cancelled = false
    setSelectedFile(null)
    setContent({ state: 'idle' })
    setExpanded({})
    setTree({})
    setActiveDir(workspace.workdir)
    setCreating(null)
    setRenaming(null)
    setFormError(null)
    setDraft('')
    void desktop.listDir(workspace.workdir).then((entries) => {
      if (!cancelled) setTree({ [workspace.workdir]: entries ?? [] })
    })
    return () => {
      cancelled = true
    }
  }, [workspace.workdir])

  // Keep the editor draft in sync with the loaded file: on open and after a
  // successful save (which produces a new content object), reset the textarea
  // to the canonical text and clear dirty.
  useEffect(() => {
    if (content.state === 'ok' && !content.truncated) {
      setDraft(content.text)
    }
  }, [content])

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

  async function openFile(entry: DirEntry): Promise<void> {
    setSelectedFile(entry.path)
    // The file's parent becomes the active target for "new …".
    setActiveDir(parentDir(entry.path))
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

  // Re-fetch the root + every currently-expanded directory in place (keeps the
  // expand/selection state; manual refresh only — no fs.watch in MVP).
  async function refresh(): Promise<void> {
    const paths = [workspace.workdir, ...Object.keys(expanded)]
    const updates: Record<string, DirEntry[]> = {}
    for (const p of paths) updates[p] = (await desktop.listDir(p)) ?? []
    setTree((t) => ({ ...t, ...updates }))
  }

  const editable = content.state === 'ok' && !content.truncated
  const dirty = editable && draft !== content.text

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
    await refresh()
    // For a new file, jump straight to editing it; for a folder, expand it so
    // the user sees (and can populate) the empty dir.
    if (creating.kind === 'file') {
      void openFile({ name, dir: false, path: newPath })
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
    // If the renamed entry (or anything under it) was the open file, follow it
    // under its new name so the viewer stays in sync.
    if (selectedFile && (selectedFile === oldPath || selectedFile.startsWith(oldPath + '\\') || selectedFile.startsWith(oldPath + '/'))) {
      if (selectedFile === oldPath) {
        const entry: DirEntry = { name: newName, dir: renaming.dir, path: newPath }
        void openFile(entry)
      } else {
        // A parent folder was renamed: the open file's path prefix changed.
        setSelectedFile(newPath + selectedFile.slice(oldPath.length))
      }
    }
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
    // Clear the viewer if the deleted entry was the open file or an ancestor
    // folder of it.
    if (
      selectedFile &&
      (selectedFile === entry.path ||
        selectedFile.startsWith(entry.path + '\\') ||
        selectedFile.startsWith(entry.path + '/'))
    ) {
      setSelectedFile(null)
      setContent({ state: 'idle' })
    }
    await refresh()
  }

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
    // New content object → the draft-sync effect resets the textarea + clears dirty.
    setContent({ ...content, text: draft })
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
        <div
          key="__create"
          className="flex items-center gap-1 py-1 pr-2"
          style={{ paddingLeft: 8 + (depth + 1) * 12 }}
        >
          <span className="w-3 shrink-0" />
          {creating.kind === 'dir' ? (
            <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
          ) : (
            <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
          )}
          <input
            autoFocus
            value={createValue}
            placeholder={creating.kind === 'dir' ? '文件夹名' : '文件名'}
            onChange={(e) => setCreateValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void confirmCreate()
              else if (e.key === 'Escape') cancelCreate()
            }}
            onBlur={cancelCreate}
            className="min-w-0 flex-1 rounded border border-sky-700/70 bg-app px-1 py-0.5 text-xs text-ink outline-none"
          />
          <button
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => void confirmCreate()}
            title="确认"
            className="rounded p-0.5 text-success transition hover:bg-elevated"
          >
            <Check className="h-3 w-3" />
          </button>
          <button
            onMouseDown={(e) => e.preventDefault()}
            onClick={cancelCreate}
            title="取消"
            className="rounded p-0.5 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )
    }
    if (entries) {
      for (const entry of entries) {
        const isOpen = !!expanded[entry.path]
        const children = tree[entry.path]
        const isRenaming = renaming?.path === entry.path
        rows.push(
          <div key={entry.path} className="group relative">
            {isRenaming ? (
              <div
                className="flex items-center gap-1 py-1 pr-2"
                style={{ paddingLeft: 8 + depth * 12 }}
              >
                {entry.dir ? (
                  <ChevronRight className="h-3 w-3 shrink-0 text-ink-4" />
                ) : (
                  <span className="w-3 shrink-0" />
                )}
                {entry.dir ? (
                  <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
                ) : (
                  <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
                )}
                <input
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void confirmRename()
                    else if (e.key === 'Escape') cancelRename()
                  }}
                  onBlur={cancelRename}
                  onFocus={(e) => e.target.select()}
                  className="min-w-0 flex-1 rounded border border-sky-700/70 bg-app px-1 py-0.5 text-xs text-ink outline-none"
                />
                <button
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => void confirmRename()}
                  title="确认"
                  className="rounded p-0.5 text-success transition hover:bg-elevated"
                >
                  <Check className="h-3 w-3" />
                </button>
                <button
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={cancelRename}
                  title="取消"
                  className="rounded p-0.5 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ) : (
              <>
                <button
                  onClick={() => (entry.dir ? void toggle(entry) : void openFile(entry))}
                  style={{ paddingLeft: 8 + depth * 12 }}
                  className={cn(
                    'flex w-full items-center gap-1 py-1 pr-16 text-left text-xs transition hover:bg-elevated/60',
                    selectedFile === entry.path && 'bg-elevated text-ink'
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
                  <span className="truncate">{entry.name}</span>
                </button>
                {/* Hover actions (per row, file or folder). pr-16 on the row
                 * above keeps the name clear of these. */}
                <div className="absolute right-1 top-1/2 flex -translate-y-1/2 gap-0.5 opacity-0 transition group-hover:opacity-100">
                  <button
                    onClick={() => {
                      setFormError(null)
                      setRenaming({ path: entry.path, dir: entry.dir, name: entry.name })
                      setRenameValue(entry.name)
                    }}
                    title="重命名"
                    className="rounded p-0.5 text-ink-4 transition hover:bg-strong hover:text-ink"
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    onClick={() => void onDelete(entry)}
                    title="删除"
                    className="rounded p-0.5 text-ink-4 transition hover:bg-strong hover:text-danger"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              </>
            )}
            {entry.dir && isOpen && depth < MAX_DEPTH &&
              renderEntries(children, depth + 1, entry.path)}
            {entry.dir && isOpen && depth >= MAX_DEPTH && (
              <div
                style={{ paddingLeft: 8 + (depth + 1) * 12 }}
                className="py-1 text-[10px] text-ink-4"
              >
                （已达深度上限）
              </div>
            )}
          </div>
        )
      }
    }
    return rows
  }

  const rootEntries = tree[workspace.workdir]

  return (
    <aside className={cn('flex flex-col bg-panel/40', className)}>
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="flex-1 truncate text-xs font-medium" title={workspace.workdir}>
          {workspace.name}
        </span>
        <button
          onClick={() => {
            setFormError(null)
            setCreating({ parent: activeDir, kind: 'file' })
            setCreateValue('')
            // Make sure the target dir is open so the inline input is visible.
            setExpanded((e) => ({ ...e, [activeDir]: true }))
            if (tree[activeDir] == null) {
              void desktop.listDir(activeDir).then((es) => setTree((t) => ({ ...t, [activeDir]: es ?? [] })))
            }
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
            if (tree[activeDir] == null) {
              void desktop.listDir(activeDir).then((es) => setTree((t) => ({ ...t, [activeDir]: es ?? [] })))
            }
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
      </div>

      {formError && (
        <div className="border-b border-danger/40 bg-danger/10 px-3 py-1 text-[11px] text-danger">
          {formError}
        </div>
      )}

      <div className="flex-1 overflow-auto py-1">
        {rootEntries == null && !(creating && creating.parent === workspace.workdir) ? (
          <div className="px-3 py-2 text-xs text-ink-4">加载中…</div>
        ) : rootEntries?.length === 0 && !(creating && creating.parent === workspace.workdir) ? (
          <div className="px-3 py-2 text-xs text-ink-4">空目录</div>
        ) : (
          renderEntries(rootEntries, 0, workspace.workdir)
        )}
      </div>

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
                  title="保存"
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
          <div className="flex-1 overflow-auto">
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
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  className="h-full min-h-full w-full resize-none bg-transparent p-3 font-mono text-[11px] leading-relaxed text-ink-2 outline-none"
                />
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
    </aside>
  )
}
