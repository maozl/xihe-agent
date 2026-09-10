import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { FileDiff, Play, X } from 'lucide-react'
import { desktop, type ReadResult } from '../lib/desktop'
import { detectLanguage } from '../lib/lang'
import { useStore } from '../store'
import { cn } from '../lib/cn'

const MonacoPane = lazy(() =>
  import('./monaco/MonacoPane').then((m) => ({ default: m.MonacoPane }))
)
const MonacoDiffPane = lazy(() =>
  import('./monaco/MonacoDiffPane').then((m) => ({ default: m.MonacoDiffPane }))
)

/** Rough interpreter guess for the Run panel prefill — a starting point the
 *  user edits before running, not a build system. */
export function guessRunCommand(path: string): string | null {
  const p = path.replace(/\\/g, '/').toLowerCase()
  if (p.endsWith('.py')) return `python "${path}"`
  if (/\.(mjs|cjs|js)$/.test(p)) return `node "${path}"`
  if (/\.(ts|tsx)$/.test(p)) return `npx tsx "${path}"`
  return null
}

type Content =
  | { state: 'loading' }
  | { state: 'ok'; text: string; truncated: boolean }
  | { state: 'binary' }
  | { state: 'error' }

/** Workbench center pane: the tab strip plus the active tab's editor.
 *  File-tab contents live here (per-path canonical text + user draft), so
 *  closing the last tab drops them; tab identity/dirty live in the store, the
 *  texts do not (they re-read from disk on reopen). */
export function EditorArea({ className }: { className?: string }) {
  const editorTabs = useStore((s) => s.editorTabs)
  const activeTabId = useStore((s) => s.activeTabId)
  const fsVersion = useStore((s) => s.fsVersion)
  const setActiveTab = useStore((s) => s.setActiveTab)
  const closeTab = useStore((s) => s.closeTab)
  const openRunPanel = useStore((s) => s.openRunPanel)

  const [files, setFiles] = useState<Record<string, Content>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // path → git HEAD text for gutter change marks ('' = untracked/new file,
  // null = no marks: not a repo / git unavailable / too large). Fetched once
  // per open — HEAD only moves on commit, which the renderer can't observe.
  const [heads, setHeads] = useState<Record<string, string | null>>({})
  // path → disk moved under a DIRTY tab (banner until the user decides).
  const [external, setExternal] = useState<Record<string, boolean>>({})
  const [saving, setSaving] = useState(false)
  // path → last save failure message (banner until the next successful save).
  const [saveFail, setSaveFail] = useState<Record<string, string>>({})

  // Snapshots for the fsVersion effect (it must not re-run on every keystroke).
  const filesRef = useRef(files)
  const draftsRef = useRef(drafts)
  filesRef.current = files
  draftsRef.current = drafts
  // Latest disk text per path — the 重新加载 action adopts this.
  const diskTextRef = useRef<Record<string, string>>({})

  const active = editorTabs.find((t) => t.id === activeTabId) ?? null
  const activeRunCmd =
    active?.kind === 'file' && active.path ? guessRunCommand(active.path) : null
  const openPaths = editorTabs
    .filter((t) => t.kind === 'file' && t.path)
    .map((t) => t.path as string)

  // Load a tab's file on first open.
  useEffect(() => {
    for (const p of openPaths) {
      if (p in filesRef.current) continue
      setFiles((f) => ({ ...f, [p]: { state: 'loading' } }))
      void desktop.readFile(p).then((r) => {
        setFiles((f) => {
          if ((f[p]?.state ?? 'loading') !== 'loading') return f
          return { ...f, [p]: mapRead(r) }
        })
        if (r.ok && !r.truncated) setDrafts((d) => (p in d ? d : { ...d, [p]: r.content }))
      })
      void desktop.gitHeadFile(p).then((r) => {
        setHeads((h) => (p in h ? h : { ...h, [p]: r.ok ? r.content ?? '' : null }))
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorTabs])

  // Agent writes / desktop-side CRUD / editor saves bump fsVersion → re-read
  // every open file. Clean tabs adopt the disk text; dirty tabs keep the draft
  // and get the banner instead (never clobber unsaved edits).
  useEffect(() => {
    if (fsVersion === 0) return
    for (const p of openPaths) {
      void desktop.readFile(p).then((r) => {
        if (!r.ok || r.truncated) return
        const cur = filesRef.current[p]
        if (cur?.state !== 'ok') {
          // Was binary/error/truncated/loading — a clean re-read is strictly
          // better unless another state already landed.
          setFiles((f) => ((f[p]?.state ?? 'loading') === 'ok' ? f : { ...f, [p]: mapRead(r) }))
          if (!(p in draftsRef.current)) setDrafts((d) => ({ ...d, [p]: r.content }))
          return
        }
        if (r.content === cur.text) return
        diskTextRef.current[p] = r.content
        if (draftsRef.current[p] !== cur.text) {
          setExternal((e) => ({ ...e, [p]: true }))
        } else {
          setFiles((f) => ({ ...f, [p]: { state: 'ok', text: r.content, truncated: false } }))
          setDrafts((d) => ({ ...d, [p]: r.content }))
        }
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsVersion])

  function mapRead(r: ReadResult): Content {
    if (!r.ok) {
      if (r.reason === 'binary') return { state: 'binary' }
      return { state: 'error' }
    }
    return { state: 'ok', text: r.content, truncated: r.truncated }
  }

  function tabDirty(path: string): boolean {
    const c = files[path]
    const d = drafts[path]
    return c?.state === 'ok' && !c.truncated && d !== undefined && d !== c.text
  }

  async function save(path: string): Promise<void> {
    const cur = files[path]
    const draft = drafts[path]
    if (!cur || cur.state !== 'ok' || cur.truncated || draft === undefined || saving) return
    if (draft === cur.text) return
    setSaving(true)
    const r = await desktop.writeFile(path, draft)
    setSaving(false)
    if (!r.ok) {
      // Rare (sandbox rejects / disk error) — banner + keep the draft editable.
      const msg =
        r.reason === 'outsideWorkspace'
          ? '路径不在工作空间内'
          : r.reason === 'notFound'
            ? '目标不存在'
            : '读写错误'
      setSaveFail((m) => ({ ...m, [path]: `保存失败：${msg}` }))
      return
    }
    setSaveFail((m) => {
      if (!m[path]) return m
      const next = { ...m }
      delete next[path]
      return next
    })
    setFiles((f) => ({ ...f, [path]: { state: 'ok', text: draft, truncated: false } }))
    setExternal((e) => {
      if (!e[path]) return e
      const next = { ...e }
      delete next[path]
      return next
    })
    useStore.getState().bumpFsVersion()
  }

  function adoptDisk(path: string): void {
    const text = diskTextRef.current[path]
    if (text === undefined) return
    setFiles((f) => ({ ...f, [path]: { state: 'ok', text, truncated: false } }))
    setDrafts((d) => ({ ...d, [path]: text }))
    setExternal((e) => {
      if (!e[path]) return e
      const next = { ...e }
      delete next[path]
      return next
    })
  }

  return (
    <section className={cn('flex min-w-0 flex-col bg-app', className)}>
      {/* Tab strip */}
      <div className="flex shrink-0 items-stretch overflow-x-auto border-b border-line bg-panel/40">
        {editorTabs.map((t) => {
          const isActive = t.id === activeTabId
          const dirty = !!(t.kind === 'file' && t.path && tabDirty(t.path))
          return (
            <div
              key={t.id}
              className={cn(
                'group/tab flex shrink-0 items-center gap-1.5 border-r border-line px-3 py-1.5 text-xs transition',
                isActive ? 'bg-app text-ink' : 'text-ink-3 hover:bg-elevated/60'
              )}
            >
              {t.kind === 'diff' && <FileDiff className="h-3 w-3 shrink-0 text-accent" />}
              <button
                onClick={() => setActiveTab(t.id)}
                className="flex max-w-40 items-center gap-1.5 truncate"
                title={t.kind === 'file' ? t.path : t.title}
              >
                <span className="truncate">{t.title}</span>
                {dirty && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-warning" />}
              </button>
              <button
                onClick={() => closeTab(t.id)}
                title="关闭"
                className="rounded p-0.5 text-ink-4 opacity-0 transition hover:bg-strong hover:text-ink group-hover/tab:opacity-100"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          )
        })}
        {activeRunCmd != null && (
          <button
            onClick={() => openRunPanel(activeRunCmd)}
            title="在运行面板中执行（按扩展名猜的命令，可修改）"
            className="ml-auto flex shrink-0 items-center gap-1 self-center rounded-md px-2 py-1 text-xs text-ink-3 transition hover:bg-elevated hover:text-ink"
          >
            <Play className="h-3 w-3" /> 运行
          </button>
        )}
      </div>

      {/* Active tab body */}
      <div className="min-h-0 flex-1">
        {!active && (
          <div className="flex h-full items-center justify-center text-xs text-ink-4">
            在左侧文件树点击文件打开编辑
          </div>
        )}
        {active?.kind === 'diff' && active.diff && (
          <Suspense fallback={<div className="p-3 text-xs text-ink-4">对比视图加载中…</div>}>
            <MonacoDiffPane
              oldText={active.diff.oldText}
              newText={active.diff.newText}
              oldHeader={active.diff.oldHeader}
              newHeader={active.diff.newHeader}
              language={active.diff.language}
            />
          </Suspense>
        )}
        {active?.kind === 'file' && active.path && <FileBody
          path={active.path}
          content={files[active.path]}
          draft={drafts[active.path]}
          headText={heads[active.path] ?? null}
          external={!!external[active.path]}
          saveFail={saveFail[active.path]}
          saving={saving}
          onDraft={(v) => setDrafts((d) => ({ ...d, [active.path as string]: v }))}
          onSave={() => void save(active.path as string)}
          onReload={() => adoptDisk(active.path as string)}
          onDismissExternal={() =>
            setExternal((e) => {
              if (!e[active.path as string]) return e
              const next = { ...e }
              delete next[active.path as string]
              return next
            })
          }
        />}
      </div>
    </section>
  )
}

interface FileBodyProps {
  path: string
  content: Content | undefined
  draft: string | undefined
  headText: string | null
  external: boolean
  saveFail: string | undefined
  saving: boolean
  onDraft: (v: string) => void
  onSave: () => void
  onReload: () => void
  onDismissExternal: () => void
}

function FileBody({
  path,
  content,
  draft,
  headText,
  external,
  saveFail,
  saving,
  onDraft,
  onSave,
  onReload,
  onDismissExternal,
}: FileBodyProps) {
  if (!content || content.state === 'loading') {
    return <div className="p-3 text-xs text-ink-4">读取中…</div>
  }
  if (content.state === 'binary') {
    return <div className="p-3 text-xs text-ink-4">二进制文件，无法编辑</div>
  }
  if (content.state === 'error') {
    return <div className="p-3 text-xs text-ink-4">读取失败（文件不存在或不可读）</div>
  }
  if (content.truncated) {
    return (
      <>
        <pre className="whitespace-pre-wrap break-all p-3 font-mono text-[11px] leading-relaxed text-ink-3">
          {content.text}
        </pre>
        <div className="border-t border-line bg-warning/5 px-3 py-1.5 text-[10px] text-warning/80">
          文件过大，仅显示前 1 MB；此处只读，请用 agent 或外部编辑器修改。
        </div>
      </>
    )
  }
  return (
    <div className="relative h-full">
      {saveFail && (
        <div className="absolute inset-x-0 top-0 z-10 border-b border-danger/40 bg-danger/10 px-3 py-1.5 text-[11px] text-danger">
          {saveFail}
        </div>
      )}
      {external && draft !== undefined && (
        <div className="absolute inset-x-0 top-0 z-10 flex items-center gap-2 border-b border-warning/40 bg-warning/10 px-3 py-1.5 text-[11px] text-warning">
          <span className="flex-1">文件已在磁盘上被修改（agent 或其他程序写入）。</span>
          <button
            onClick={onReload}
            className="rounded px-1.5 py-0.5 transition hover:bg-strong"
            title="放弃当前草稿，改用磁盘上的新内容"
          >
            用磁盘内容
          </button>
          <button
            onClick={onDismissExternal}
            className="rounded px-1.5 py-0.5 transition hover:bg-strong"
            title="保留草稿，保存时将覆盖磁盘内容"
          >
            保留我的
          </button>
        </div>
      )}
      <Suspense fallback={<div className="p-3 text-xs text-ink-4">编辑器加载中…</div>}>
        <MonacoPane
          path={path}
          value={draft ?? content.text}
          language={detectLanguage(path)}
          onChange={onDraft}
          onSave={onSave}
          headText={headText}
          minimap
        />
      </Suspense>
      {saving && (
        <div className="absolute bottom-2 right-3 rounded bg-panel px-2 py-0.5 text-[10px] text-ink-3">
          保存中…
        </div>
      )}
    </div>
  )
}
