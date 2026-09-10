import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  BookOpen,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useStore } from '../store'
import { cn } from '../lib/cn'
import { Markdown } from './Markdown'
import {
  deleteMemory,
  getKbsPage,
  listKbs,
  listMemory,
  listSpecialists,
  putMemory,
  type KbsCandidate,
  type KbsPage,
  type KbsView,
  type MemoryEntry,
} from '../lib/serveClient'

// Full-window page over serve's /memory + /kbs endpoints
// (serve/knowledge.py).
// Memory is the same store the agent tools use — every edit is the same
// scan/atomic-write path the agent takes. KBS is strictly read-only browsing.

function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line p-4 text-center text-xs text-ink-4">
      {children}
    </div>
  )
}

function Tag({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: 'neutral' | 'sky' | 'emerald' | 'amber'
}) {
  const cls = {
    neutral: 'border-line-strong text-ink-3',
    sky: 'border-accent/50 text-accent',
    emerald: 'border-success/50 text-success',
    amber: 'border-warning/50 text-warning',
  }[tone]
  return <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', cls)}>{children}</span>
}

function StatusBanner({ status }: { status: { kind: 'ok' | 'err'; msg: string } | null }) {
  if (!status) return null
  return (
    <div
      className={cn(
        'rounded-lg border px-3 py-2 text-xs',
        status.kind === 'ok'
          ? 'border-success/40 bg-success/5 text-success'
          : 'border-danger/40 bg-danger/5 text-danger'
      )}
    >
      {status.msg}
    </div>
  )
}

const INPUT_CLS =
  'w-full rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm text-ink outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30 placeholder:text-ink-4'

/** `agent:<slug>:` key prefix = specialist-owned memory (prompt convention on
 *  the agent side; this UI groups on it). */
const NS_RE = /^agent:([^:]+):/

interface MemGroup {
  id: string
  label: string
  entries: MemoryEntry[]
}

function MemorySection({ refreshKey }: { refreshKey: number }) {
  const serveConnected = useStore((s) => s.serveConnected)
  const [entries, setEntries] = useState<MemoryEntry[] | null>(null)
  const [corrupt, setCorrupt] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [query, setQuery] = useState('')
  // key being edited, '__new__' while creating, null = list view
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<{ key: string; value: string; previousKey?: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null)
  const [specialistNames, setSpecialistNames] = useState<Record<string, string>>({})
  // keys whose value block is un-clamped (3-line clamp otherwise)
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set())

  const load = useCallback(async () => {
    const r = await listMemory()
    if (r) {
      setEntries(r.items)
      setCorrupt(r.corrupt)
    }
    setLoaded(true)
  }, [])

  useEffect(() => {
    if (serveConnected) void load()
  }, [serveConnected, load, refreshKey])

  useEffect(() => {
    if (!serveConnected) return
    void listSpecialists().then((r) => {
      if (!r) return
      const names: Record<string, string> = {}
      for (const sp of r.specialists) names[sp.slug] = sp.spec.name || sp.slug
      setSpecialistNames(names)
    })
  }, [serveConnected])

  const groups = useMemo<MemGroup[]>(() => {
    const q = query.trim().toLowerCase()
    const src = (entries ?? []).filter(
      (e) => !q || e.key.toLowerCase().includes(q) || e.value.toLowerCase().includes(q)
    )
    const main: MemoryEntry[] = []
    const bySlug = new Map<string, MemoryEntry[]>()
    for (const e of src) {
      const m = NS_RE.exec(e.key)
      if (!m) {
        main.push(e)
        continue
      }
      const list = bySlug.get(m[1]) ?? []
      list.push(e)
      bySlug.set(m[1], list)
    }
    const out: MemGroup[] = [{ id: 'main', label: `主 Agent（${main.length}）`, entries: main }]
    for (const slug of [...bySlug.keys()].sort()) {
      const items = bySlug.get(slug) ?? []
      const name = specialistNames[slug]
      out.push({ id: slug, label: `${name ?? `agent:${slug}`}（${items.length}）`, entries: items })
    }
    return out.filter((g) => g.entries.length > 0 || g.id === 'main')
  }, [entries, query, specialistNames])

  const beginEdit = (e: MemoryEntry) => {
    setEditing(e.key)
    setDraft({ key: e.key, value: e.value, previousKey: e.key })
    setStatus(null)
  }

  const beginCreate = () => {
    setEditing('__new__')
    setDraft({ key: '', value: '' })
    setStatus(null)
  }

  const cancel = () => {
    setEditing(null)
    setDraft(null)
  }

  const save = async (forceCorrupt = false) => {
    if (!draft) return
    const key = draft.key.trim()
    if (!key) {
      setStatus({ kind: 'err', msg: 'key 不能为空' })
      return
    }
    const prev = draft.previousKey
    if (entries?.some((e) => e.key === key && e.key !== prev)) {
      setStatus({ kind: 'err', msg: `key "${key}" 已存在` })
      return
    }
    setSaving(true)
    const r = await putMemory(key, draft.value, prev, forceCorrupt)
    setSaving(false)
    if (!r) {
      setStatus({ kind: 'err', msg: '保存失败（serve 未连接）' })
      return
    }
    if (!r.ok) {
      if (!forceCorrupt && (r.error ?? '').includes('解析失败')) {
        if (window.confirm('原 memories.json 已损坏，强制保存将丢弃其全部内容。继续？')) {
          void save(true)
        }
        return
      }
      setStatus({ kind: 'err', msg: r.error ?? '保存失败' })
      return
    }
    setStatus({ kind: 'ok', msg: prev && prev !== key ? `已保存（${prev} → ${key}）` : '已保存' })
    cancel()
    await load()
  }

  const remove = async (key: string, force = false) => {
    if (!force && !window.confirm(`删除记忆 "${key}"？`)) return
    const r = await deleteMemory(key, force)
    if (!r) {
      setStatus({ kind: 'err', msg: '删除失败（serve 未连接）' })
      return
    }
    if (!r.ok) {
      if (!force && (r.error ?? '').includes('解析失败')) {
        if (window.confirm('原 memories.json 已损坏。仍要继续删除吗？')) void remove(key, true)
        return
      }
      setStatus({ kind: 'err', msg: r.error ?? '删除失败' })
      return
    }
    setStatus({ kind: 'ok', msg: r.deleted ? `已删除 ${key}` : `${key} 本就不存在` })
    await load()
  }

  if (!serveConnected) return <Empty>xihe 未连接。</Empty>
  if (!loaded) return <Empty>读取中…</Empty>
  if (entries === null)
    return <Empty>接口不可用——xihe 版本过旧，重启 xihe 后重试。</Empty>

  const draftForm = (
    <div className="mt-1 space-y-2 rounded-lg border border-line-strong bg-panel/60 p-3">
      <div className="flex items-center gap-2">
        <input
          value={draft?.key ?? ''}
          onChange={(e) => setDraft((d) => (d ? { ...d, key: e.target.value } : d))}
          placeholder="key（专家记忆用 agent:<slug>: 前缀）"
          className={cn(INPUT_CLS, 'font-mono text-xs')}
          autoFocus={editing === '__new__'}
        />
      </div>
      <textarea
        value={draft?.value ?? ''}
        onChange={(e) => setDraft((d) => (d ? { ...d, value: e.target.value } : d))}
        placeholder="内容（纯文本）"
        className={cn(INPUT_CLS, 'min-h-[96px] font-mono text-xs')}
      />
      <div className="flex items-center justify-end gap-2">
        <button onClick={cancel} className="rounded-lg px-3 py-1.5 text-xs text-ink-3 transition hover:bg-elevated hover:text-ink">
          取消
        </button>
        <button
          onClick={() => void save()}
          disabled={saving}
          className="rounded-lg bg-brand px-3 py-1.5 text-xs text-white transition hover:opacity-90 disabled:opacity-40"
        >
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
  )

  return (
    <div className="flex h-full flex-col">
      {corrupt && (
        <div className="border-b border-warning/40 bg-warning/5 px-5 py-2 text-xs text-warning">
          memories.json 解析失败——写入已被服务端锁定。可从 memories.json.bak 恢复；强制保存将丢弃损坏内容。
        </div>
      )}
      <div className="flex items-center gap-2 px-5 py-2.5">
        <div className="flex w-64 items-center gap-2 rounded-lg border border-line bg-panel/60 px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 shrink-0 text-ink-4" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索 key / 内容"
            className="w-full bg-transparent text-xs text-ink outline-none placeholder:text-ink-4"
          />
          {query && (
            <button onClick={() => setQuery('')} className="text-ink-4 transition hover:text-ink-2">
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
        <span className="text-xs text-ink-4">{entries.length} 条</span>
        <button
          onClick={beginCreate}
          className="ml-auto flex items-center gap-1 rounded-lg border border-line bg-panel/60 px-2.5 py-1.5 text-xs text-ink-2 transition hover:bg-elevated"
        >
          <Plus className="h-3.5 w-3.5" />
          新增
        </button>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 pb-5">
        <StatusBanner status={status} />
        {editing === '__new__' && draftForm}
        {groups.map((g) => (
          <div key={g.id}>
            <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-ink-3">
              {g.label}
            </div>
            <div className="space-y-1.5">
              {g.entries.length === 0 && <Empty>（空）</Empty>}
              {g.entries.map((e) => {
                // Inside a specialist group the `agent:<slug>:` prefix is
                // redundant (the header already names it) — dim it, keep it
                // copyable in the title.
                const prefix = g.id === 'main' ? '' : `agent:${g.id}:`
                return (
                  <div key={e.key}>
                    <div
                      className={cn(
                        'group rounded-lg border border-line bg-panel/40 px-3 py-2 transition hover:border-line-strong',
                        editing === e.key && 'border-brand/50'
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className="min-w-0 flex-1 truncate font-mono text-xs text-ink"
                          title={e.key}
                        >
                          {prefix && <span className="text-ink-4">{prefix}</span>}
                          {e.key.slice(prefix.length)}
                        </span>
                        <div className="flex shrink-0 items-center gap-1 opacity-0 transition group-hover:opacity-100">
                          <button
                            onClick={() => beginEdit(e)}
                            title="编辑"
                            className="rounded p-0.5 text-ink-4 transition hover:text-ink"
                          >
                            <Pencil className="h-3 w-3" />
                          </button>
                          <button
                            onClick={() => void remove(e.key)}
                            title="删除"
                            className="rounded p-0.5 text-ink-4 transition hover:text-danger"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      </div>
                      {(() => {
                        const isExp = expandedKeys.has(e.key)
                        // ~3 clamped lines of prose, or multi-line content past
                        // the clamp — anything shorter needs no expand toggle
                        const tooLong =
                          e.value.length > 150 || e.value.split('\n').length > 3
                        return (
                          <>
                            <div
                              className={cn(
                                'mt-1 break-words whitespace-pre-wrap text-xs leading-relaxed text-ink-3',
                                !isExp && tooLong && 'line-clamp-3'
                              )}
                            >
                              {e.value || <span className="text-ink-4">（空值）</span>}
                            </div>
                            {tooLong && (
                              <button
                                type="button"
                                onClick={() =>
                                  setExpandedKeys((s) => {
                                    const next = new Set(s)
                                    if (next.has(e.key)) next.delete(e.key)
                                    else next.add(e.key)
                                    return next
                                  })
                                }
                                className="mt-0.5 text-[10px] text-ink-4 hover:text-ink-3"
                              >
                                {isExp ? '收起' : '展开全文'}
                              </button>
                            )}
                          </>
                        )
                      })()}
                    </div>
                    {editing === e.key && draftForm}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---- kbs directory tree ----------------------------------------------------

interface KbsNode {
  name: string
  path: string
  isDir: boolean
  children: KbsNode[]
  file?: { rel: string; title: string; hint?: string; status?: KbsCandidate['status'] }
}

/** Tree mirroring the on-disk .biz_kbs layout, built from the inventory's
 *  root-relative paths (wiki core files + domain hubs + entity/insight/concept
 *  pages + meta/candidates). */
function buildKbsTree(view: KbsView): KbsNode[] {
  const root: KbsNode = { name: '', path: '', isDir: true, children: [] }
  const dirFor = (node: KbsNode, seg: string): KbsNode => {
    let d = node.children.find((c) => c.isDir && c.name === seg)
    if (!d) {
      d = { name: seg, path: node.path ? `${node.path}/${seg}` : seg, isDir: true, children: [] }
      node.children.push(d)
    }
    return d
  }
  const addFile = (rel: string, title: string, hint?: string, status?: KbsCandidate['status']) => {
    const segs = rel.split('/')
    const dir = segs.slice(0, -1).reduce(dirFor, root)
    dir.children.push({
      name: segs[segs.length - 1], path: rel, isDir: false, children: [],
      file: { rel, title, hint, status },
    })
  }
  for (const w of view.wiki) addFile(w.rel, w.title)
  for (const p of view.pages) addFile(p.rel, p.title, p.summary)
  for (const c of view.candidates) addFile(c.rel, c.title, c.next_action, c.status)

  const sortRec = (n: KbsNode) => {
    n.children.sort((a, b) =>
      a.isDir === b.isDir ? a.name.localeCompare(b.name) : a.isDir ? -1 : 1)
    n.children.forEach(sortRec)
  }
  sortRec(root)
  return root.children
}

function filterTree(nodes: KbsNode[], q: string): KbsNode[] {
  const out: KbsNode[] = []
  for (const n of nodes) {
    if (n.isDir) {
      const kids = filterTree(n.children, q)
      if (kids.length) out.push({ ...n, children: kids })
    } else {
      const f = n.file!
      if ([f.title, f.rel, f.hint ?? ''].some((s) => s.toLowerCase().includes(q))) out.push(n)
    }
  }
  return out
}

function countFiles(n: KbsNode): number {
  return n.isDir ? n.children.reduce((s, c) => s + countFiles(c), 0) : 1
}

function KbsSection({ refreshKey }: { refreshKey: number }) {
  const serveConnected = useStore((s) => s.serveConnected)
  const [view, setView] = useState<KbsView | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [query, setQuery] = useState('')
  // paths the user collapsed; absent = expanded (the whole tree opens by
  // default — a KBS is small enough to read as structure)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<{ rel: string; title: string } | null>(null)
  const [page, setPage] = useState<KbsPage | null>(null)
  const [loadingPage, setLoadingPage] = useState(false)
  // raw-source view (frontmatter included); sticky across page selections
  const [raw, setRaw] = useState(false)
  const cache = useRef(new Map<string, KbsPage>())

  const load = useCallback(async () => {
    const r = await listKbs()
    if (r) setView(r)
    setLoaded(true)
  }, [])

  useEffect(() => {
    if (serveConnected) void load()
  }, [serveConnected, load, refreshKey])

  const select = useCallback(async (rel: string, title: string) => {
    setSelected({ rel, title })
    const cached = cache.current.get(rel)
    if (cached) {
      setPage(cached)
      return
    }
    setLoadingPage(true)
    setPage(null)
    const r = await getKbsPage(rel)
    setLoadingPage(false)
    cache.current.set(rel, r ?? { ok: false, error: '读取失败（serve 未连接）' })
    setPage(r)
  }, [])

  const q = query.trim().toLowerCase()
  // While searching, every level renders open so matches stay visible.
  const tree = useMemo(() => (view ? filterTree(buildKbsTree(view), q) : []), [view, q])

  if (!serveConnected) return <Empty>xihe 未连接。</Empty>
  if (!loaded) return <Empty>读取中…</Empty>
  if (view === null) return <Empty>接口不可用——xihe 版本过旧，重启 xihe 后重试。</Empty>

  if (!view.enabled || !view.initialized) {
    return (
      <div className="space-y-3 p-5">
        {!view.enabled && (
          <Empty>config.yaml 的 kbs.enabled 未开启——KBS 工具未注册。开启后重启 xihe。</Empty>
        )}
        {view.enabled && !view.initialized && (
          <Empty>KBS 未初始化（{view.root} 不存在）。让 agent 运行 kbs_init 创建。</Empty>
        )}
      </div>
    )
  }

  const st = view.status
  const lint = st?.lint_status ?? {}

  const toggle = (path: string) =>
    setCollapsed((s) => {
      const next = new Set(s)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const renderNode = (node: KbsNode, depth: number): ReactNode => {
    if (node.isDir) {
      const open = q ? true : !collapsed.has(node.path)
      return (
        <div key={node.path}>
          <button
            onClick={() => toggle(node.path)}
            style={{ paddingLeft: 8 + depth * 12 }}
            className="flex w-full items-center gap-1 py-1 pr-2 text-left text-xs text-ink-3 transition hover:bg-elevated/60 hover:text-ink"
          >
            {open ? (
              <ChevronDown className="h-3 w-3 shrink-0 text-ink-4" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0 text-ink-4" />
            )}
            {open ? (
              <FolderOpen className="h-3.5 w-3.5 shrink-0 text-accent" />
            ) : (
              <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
            )}
            <span className="truncate font-medium">{node.name}</span>
            <span className="text-[10px] text-ink-4">{countFiles(node)}</span>
          </button>
          {open && node.children.map((c) => renderNode(c, depth + 1))}
        </div>
      )
    }
    const f = node.file!
    return (
      <button
        key={node.path}
        onClick={() => void select(f.rel, f.title)}
        style={{ paddingLeft: 20 + depth * 12 }}
        className={cn(
          'flex w-full items-center gap-1 py-1 pr-2 text-left text-xs transition hover:bg-elevated/60',
          selected?.rel === f.rel ? 'bg-elevated text-ink' : 'text-ink-2'
        )}
        title={f.rel}
      >
        <span className="w-3 shrink-0" />
        <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-4" />
        <span className="min-w-0 flex-1 truncate">{f.title}</span>
        {f.status && (
          <Tag
            tone={
              f.status === 'open'
                ? 'amber'
                : f.status === 'promoted' || f.status === 'merged'
                  ? 'emerald'
                  : 'neutral'
            }
          >
            {f.status}
          </Tag>
        )}
      </button>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-5 py-2.5">
        <Tag tone="sky">{view.pages.length} 页面</Tag>
        <Tag>{view.candidates.length} 候选</Tag>
        <Tag>lint {String(lint.lint_count ?? '—')}</Tag>
        {st?.last_lint_age_hours != null && (
          <Tag tone={st.lint_stale ? 'amber' : 'neutral'}>
            上次 lint {st.last_lint_age_hours}h 前
          </Tag>
        )}
        {st?.recent_update_rows != null && <Tag>recent {st.recent_update_rows} 行</Tag>}
        {st?.hints?.length ? (
          <span className="text-[10px] text-ink-4" title={st.hints.join('\n')}>
            {st.message}
          </span>
        ) : null}
      </div>
      <div className="flex items-center gap-2 px-5 py-2.5">
        <div className="flex w-64 items-center gap-2 rounded-lg border border-line bg-panel/60 px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 shrink-0 text-ink-4" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题 / 路径 / 摘要"
            className="w-full bg-transparent text-xs text-ink outline-none placeholder:text-ink-4"
          />
          {query && (
            <button onClick={() => setQuery('')} className="text-ink-4 transition hover:text-ink-2">
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        <aside className="w-80 shrink-0 overflow-y-auto border-r border-line py-1.5">
          {tree.length === 0 ? <Empty>无匹配页面</Empty> : tree.map((n) => renderNode(n, 0))}
        </aside>
        <div className="min-w-0 flex-1 overflow-y-auto p-5">
          {!selected && <Empty>选择左侧页面查看内容</Empty>}
          {selected && loadingPage && <Empty>读取中…</Empty>}
          {selected && !loadingPage && page && !page.ok && (
            <Empty>{page.error ?? '读取失败'}</Empty>
          )}
          {selected && !loadingPage && page?.ok && (
            <div className="space-y-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h2 className="min-w-0 truncate text-sm font-semibold text-ink">
                    {selected.title}
                  </h2>
                  <button
                    onClick={() => setRaw((v) => !v)}
                    className="shrink-0 rounded border border-line px-1.5 py-0.5 text-[10px] text-ink-3 transition hover:border-line-strong hover:text-ink"
                  >
                    {raw ? '渲染显示' : '查看原文'}
                  </button>
                </div>
                <div className="mt-0.5 font-mono text-[10px] text-ink-4">{selected.rel}</div>
              </div>
              {raw ? (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-ink-2">
                  {page.content ?? ''}
                </pre>
              ) : (
                <Markdown content={page.content ?? ''} />
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export function KnowledgePage({ onBack }: { onBack: () => void }) {
  const [seg, setSeg] = useState<'memory' | 'kbs'>('memory')
  const [refreshKey, setRefreshKey] = useState(0)

  return (
    <div className="flex h-screen flex-col bg-app text-ink">
      <header className="flex items-center gap-3 border-b border-line px-5 py-3">
        <button
          onClick={onBack}
          title="返回"
          className="rounded-lg p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <BookOpen className="h-4 w-4 text-ink-3" />
        <h1 className="text-sm font-semibold">记忆 / 知识库</h1>
        <div className="flex items-center rounded-lg border border-line bg-panel/60 p-0.5">
          {(
            [
              ['memory', '记忆'],
              ['kbs', '知识库'],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              onClick={() => setSeg(id)}
              className={cn(
                'rounded-md px-2.5 py-1 text-xs transition',
                seg === id ? 'bg-accent/10 text-accent' : 'text-ink-3 hover:bg-elevated hover:text-ink-2'
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          onClick={() => setRefreshKey((k) => k + 1)}
          title="刷新"
          className="ml-auto rounded p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-hidden">
        {seg === 'memory' ? (
          <MemorySection refreshKey={refreshKey} />
        ) : (
          <KbsSection refreshKey={refreshKey} />
        )}
      </div>
    </div>
  )
}
