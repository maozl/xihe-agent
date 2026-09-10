import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  Download,
  Layers,
  RefreshCw,
  Search,
  ShoppingBag,
  Trash2,
  X,
} from 'lucide-react'
import { useStore } from '../store'
import { cn } from '../lib/cn'
import {
  installStoreItem,
  listMcp,
  listSpecialists,
  listStore,
  mountStoreItem,
  refreshStore,
  uninstallStoreItem,
  type McpServer,
  type SpecialistEntry,
  type StoreItem,
} from '../lib/serveClient'

type TypeFilter = 'all' | 'skill' | 'mcp'
type StateFilter = 'all' | 'installed'

/** Halo-style filter dropdown: trigger button + floating panel with hover
 *  highlight and a Check on the active option; closes on outside click.
 *  Self-contained open state — the parent only owns value/onChange. */
function FilterDropdown<T extends string>({
  value,
  onChange,
  options,
  icon,
}: {
  value: T
  onChange: (v: T) => void
  options: { id: T; label: string; hint?: string }[]
  icon: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const current = options.find((o) => o.id === value)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-lg border border-line bg-panel/60 px-2.5 py-1.5 text-xs text-ink-2 transition hover:bg-elevated"
      >
        <span className="text-ink-4">{icon}</span>
        <span>{current?.label ?? ''}</span>
        <ChevronDown className={cn('h-3 w-3 text-ink-4 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-44 overflow-hidden rounded-xl border border-line bg-panel shadow-xl">
          {options.map((o) => (
            <button
              key={o.id}
              onClick={() => {
                onChange(o.id)
                setOpen(false)
              }}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors',
                value === o.id
                  ? 'bg-accent/10 text-accent'
                  : 'text-ink-2 hover:bg-elevated'
              )}
            >
              <span className="flex-1">
                {o.label}
                {o.hint && <span className="ml-1.5 text-[10px] text-ink-4">{o.hint}</span>}
              </span>
              {value === o.id && <Check className="h-3.5 w-3.5 shrink-0" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** Full-window store page — browse/install catalog skills & MCP servers over
 *  serve's /store endpoints. Installs are desktop-only (no agent-side store
 *  tools); "给谁用" is expressed as ledger mounts, never as yaml edits. */
export function StorePage({ onBack }: { onBack: () => void }) {
  const serveConnected = useStore((s) => s.serveConnected)
  const [items, setItems] = useState<StoreItem[] | null>(null)
  const [sources, setSources] = useState<{ name: string; ok: boolean; error: string | null }[]>([])
  const [specialists, setSpecialists] = useState<SpecialistEntry[]>([])
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const [busy, setBusy] = useState<string | null>(null)
  const [status, setStatus] = useState<null | { kind: 'ok' | 'err'; msg: string }>(null)
  // item being installed/updated — null = list view only
  const [dialog, setDialog] = useState<StoreItem | null>(null)
  const [mountOnly, setMountOnly] = useState<StoreItem | null>(null)
  // Live MCP connection state, joined into installed MCP cards (connection
  // happens in the background after install, so the catalog alone can't say
  // whether a server actually registered its tools).
  const [mcpLive, setMcpLive] = useState<McpServer[] | null>(null)

  const load = useCallback(async () => {
    const r = await listStore()
    if (r) {
      setItems(r.items)
      setSources(r.sources ?? [])
    }
    const sp = await listSpecialists()
    if (sp) setSpecialists(sp.specialists)
    const mcp = await listMcp()
    setMcpLive(mcp)
  }, [])

  useEffect(() => {
    if (serveConnected) void load()
  }, [serveConnected, load])

  const doRefresh = async () => {
    setBusy('__refresh__')
    const r = await refreshStore()
    setBusy(null)
    if (r) {
      setItems(r.items)
      setSources(r.sources ?? [])
      setStatus({ kind: 'ok', msg: '目录已刷新' })
    } else {
      setStatus({ kind: 'err', msg: '刷新失败（serve 未连接）' })
    }
  }

  const doUninstall = async (item: StoreItem) => {
    if (!window.confirm(`卸载 ${item.title}（${item.type}）？挂载记录会一并清除。`)) return
    setBusy(`${item.type}:${item.id}`)
    const r = await uninstallStoreItem(item.type, item.id)
    setBusy(null)
    if (!r) {
      setStatus({ kind: 'err', msg: '卸载失败（serve 未连接）' })
      return
    }
    if (!r.ok) {
      setStatus({ kind: 'err', msg: r.error ?? '卸载失败' })
      return
    }
    setStatus({ kind: 'ok', msg: `已卸载 ${item.title}` })
    await load()
  }

  const q = query.trim().toLowerCase()
  const filtered = (items ?? []).filter((it) => {
    if (typeFilter !== 'all' && it.type !== typeFilter) return false
    if (stateFilter === 'installed' && !it.installed) return false
    if (!q) return true
    return [it.title, it.description, it.id, it.source_name]
      .filter(Boolean)
      .some((f) => String(f).toLowerCase().includes(q))
  })

  const agentOptions = [
    { key: 'main', label: '主 agent', hint: '需重启 serve 生效' },
    ...specialists.map((sp) => ({
      key: sp.slug,
      label: sp.spec.name || sp.slug,
      hint: '下次调用即生效',
    })),
  ]

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
        <ShoppingBag className="h-4 w-4 text-ink-3" />
        <h1 className="text-sm font-semibold">商店</h1>
        <span className="text-xs text-ink-4">
          安装 skill 与 MCP 服务器（安装即全局，挂载决定给谁用）
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {sources.map((s) => (
            <span
              key={s.name}
              title={s.ok ? s.name : `${s.name}：${s.error}`}
              className={cn(
                'flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px]',
                s.ok
                  ? 'border-line text-ink-4'
                  : 'border-danger/50 text-danger/80'
              )}
            >
              <span
                className={cn(
                  'h-1.5 w-1.5 rounded-full',
                  s.ok ? 'bg-success' : 'bg-danger'
                )}
              />
              {s.name}
            </span>
          ))}
          <button
            onClick={() => void doRefresh()}
            disabled={busy === '__refresh__'}
            title="重新拉取目录源"
            className="rounded p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink disabled:opacity-40"
          >
            <RefreshCw className={cn('h-4 w-4', busy === '__refresh__' && 'animate-spin')} />
          </button>
        </div>
      </header>

      <div className="flex items-center gap-2 border-b border-line px-5 py-2.5">
        <div className="flex w-64 items-center gap-2 rounded-lg border border-line bg-panel/60 px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 shrink-0 text-ink-4" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题 / 描述 / id"
            className="w-full bg-transparent text-xs text-ink outline-none placeholder:text-ink-4"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              className="text-ink-4 transition hover:text-ink-2"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
        {/* Install-state dropdown (all/installed) × type dropdown (all/skill/
            mcp) — two independent single-selects, combined in the filter. */}
        <FilterDropdown
          value={stateFilter}
          onChange={setStateFilter}
          icon={<Download className="h-3 w-3" />}
          options={[
            { id: 'all' as StateFilter, label: '全部' },
            { id: 'installed' as StateFilter, label: '已安装' },
          ]}
        />
        <FilterDropdown
          value={typeFilter}
          onChange={setTypeFilter}
          icon={<Layers className="h-3 w-3" />}
          options={[
            { id: 'all' as TypeFilter, label: '全部' },
            { id: 'skill' as TypeFilter, label: 'Skill' },
            { id: 'mcp' as TypeFilter, label: 'MCP' },
          ]}
        />
        {items && (
          <span className="ml-auto text-[11px] text-ink-4">{filtered.length} 条</span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {!serveConnected ? (
          <Empty>xihe未连接。</Empty>
        ) : items === null ? (
          <Empty>读取中…</Empty>
        ) : filtered.length === 0 ? (
          <Empty>
            {items.length === 0
              ? '目录为空——在 config.yaml 的 store.sources 里配置源后刷新。'
              : '没有匹配的条目。'}
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-3 2xl:grid-cols-2">
            {filtered.map((it) => (
              <ItemCard
                key={`${it.type}:${it.id}`}
                item={it}
                agentOptions={agentOptions}
                mcpLive={mcpLive?.find((s) => s.name === it.id) ?? null}
                busy={busy === `${it.type}:${it.id}`}
                onInstall={() => setDialog(it)}
                onMount={() => setMountOnly(it)}
                onUninstall={() => void doUninstall(it)}
              />
            ))}
          </div>
        )}
      </div>

      {status && (
        <div className="border-t border-line px-5 py-2">
          <div
            className={cn(
              'rounded-lg border px-3 py-1.5 text-[11px]',
              status.kind === 'ok'
                ? 'border-success/50 text-success/80'
                : 'border-danger/50 text-danger/80'
            )}
          >
            {status.msg}
          </div>
        </div>
      )}

      {dialog && (
        <InstallDialog
          item={dialog}
          agentOptions={agentOptions}
          isUpdate={dialog.installed}
          onCancel={() => setDialog(null)}
          onDone={(msg) => {
            setDialog(null)
            setStatus(msg)
            void load()
          }}
        />
      )}

      {mountOnly && (
        <MountDialog
          item={mountOnly}
          agentOptions={agentOptions}
          onCancel={() => setMountOnly(null)}
          onDone={(msg) => {
            setMountOnly(null)
            setStatus(msg)
            void load()
          }}
        />
      )}
    </div>
  )
}

function Dot({ on }: { on: boolean }) {
  return (
    <span
      className={cn(
        'h-2 w-2 shrink-0 rounded-full',
        on ? 'bg-success' : 'bg-strong'
      )}
    />
  )
}

function ItemCard({
  item,
  agentOptions,
  mcpLive,
  busy,
  onInstall,
  onMount,
  onUninstall,
}: {
  item: StoreItem
  agentOptions: { key: string; label: string; hint: string }[]
  /** Live serve-side state for this MCP server (null = skill / unknown). */
  mcpLive: McpServer | null
  busy: boolean
  onInstall: () => void
  onMount: () => void
  onUninstall: () => void
}) {
  const installing = !item.installed
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-line bg-panel/40 p-4">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            'rounded border px-1.5 py-0.5 text-[10px]',
            item.type === 'skill'
              ? 'border-accent/50 text-accent'
              : 'border-violet-500/50 text-violet-500'
          )}
        >
          {item.type === 'skill' ? 'Skill' : 'MCP'}
        </span>
        <span className="text-sm font-medium text-ink">{item.title}</span>
        <span className="text-[10px] text-ink-4">{item.id}</span>
        <div className="ml-auto flex items-center gap-1.5">
          {item.manual ? (
            <>
              <Tag tone="amber">手动配置</Tag>
              {item.type === 'mcp' && mcpLive && (
                <span
                  className="flex items-center gap-1 rounded border border-success/50 px-1.5 py-0.5 text-[10px] text-success"
                  title={mcpLive.connected ? `已连接 · 注册 ${mcpLive.tools} 个工具` : '未连接（见 agent.log）'}
                >
                  <Dot on={mcpLive.connected} />
                  {mcpLive.connected ? `${mcpLive.tools} 工具` : '未连接'}
                </span>
              )}
            </>
          ) : (
            <>
              {item.orphan && <Tag tone="amber">源已下架</Tag>}
              {item.hand_installed && <Tag tone="amber">手动放置</Tag>}
              {item.installed && item.upgradable && <Tag tone="sky">可升级</Tag>}
              {item.installed && !item.upgradable && !item.orphan && (
                mcpLive ? (
                  <span
                    className="flex items-center gap-1 rounded border border-success/50 px-1.5 py-0.5 text-[10px] text-success"
                    title={mcpLive.connected ? `已连接 · 注册 ${mcpLive.tools} 个工具` : '已安装但未连接（见 agent.log）'}
                  >
                    <Dot on={mcpLive.connected} />
                    {mcpLive.connected ? `已连接 · ${mcpLive.tools} 工具` : '未连接'}
                  </span>
                ) : (
                  <Tag tone="emerald">已安装</Tag>
                )
              )}
            </>
          )}
        </div>
      </div>

      <p className="min-h-[2rem] text-xs leading-relaxed text-ink-3">
        {item.description || '（无描述）'}
      </p>

      <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-ink-4">
        {item.source_name && <span>来源：{item.source_name}</span>}
        {item.installed_version && <span>已装 v{item.installed_version}</span>}
        {item.installed && item.version && item.version !== item.installed_version && (
          <span className="text-ink-4">目录 v{item.version}</span>
        )}
        {!item.installed && item.version && <span>v{item.version}</span>}
      </div>

      {item.unsupported && (
        <div className="flex items-center gap-1.5 rounded-lg border border-warning/50 bg-warning/10 px-2.5 py-1.5 text-[11px] text-warning/80">
          <AlertTriangle className="h-3 w-3 shrink-0" />
          {item.unsupported}
        </div>
      )}

      {item.installed && (item.mounted?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <Layers className="h-3 w-3 text-ink-4" />
          {item.mounted!.map((k) => (
            <Tag key={k}>
              {agentOptions.find((a) => a.key === k)?.label ?? k}
            </Tag>
          ))}
        </div>
      )}

      <div className="mt-auto flex items-center gap-2">
        {item.manual ? (
          <span className="text-[10px] text-ink-4">
            {item.type === 'mcp'
              ? '配置在 config.yaml 的 mcp_servers — 编辑文件管理'
              : '位于 ~/.xihe-agent/skills/ — 文件管理'}
          </span>
        ) : installing ? (
          <button
            onClick={onInstall}
            disabled={busy || !!item.unsupported}
            className="flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent transition hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Download className="h-3.5 w-3.5" />
            安装
          </button>
        ) : (
          <>
            <button
              onClick={onInstall}
              disabled={busy}
              title={item.upgradable ? '更新到目录版本' : '重装 / 更新凭据'}
              className={cn(
                'flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-40',
                item.upgradable
                  ? 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20'
                  : 'border-line-strong text-ink-2 hover:bg-elevated'
              )}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              {item.upgradable ? '更新' : '重装'}
            </button>
            <button
              onClick={onMount}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-2 transition hover:bg-elevated disabled:opacity-40"
            >
              <Layers className="h-3.5 w-3.5" />
              挂载
            </button>
            <button
              onClick={onUninstall}
              disabled={busy}
              className="ml-auto flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs text-ink-3 transition hover:border-danger/50 hover:text-danger disabled:opacity-40"
            >
              <Trash2 className="h-3.5 w-3.5" />
              卸载
            </button>
          </>
        )}
      </div>
    </div>
  )
}

/** Modal overlay shared by the install/mount dialogs. */
function Overlay({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <div className="w-full max-w-md rounded-xl border border-line bg-panel shadow-2xl">
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <button
            onClick={onClose}
            className="ml-auto rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-4 py-3">{children}</div>
      </div>
    </div>
  )
}

/** Install / update dialog: MCP credential fields (write-only — a stored key
 *  shows "已保存" and blank = inherit) + mount checkboxes. Mounts are written
 *  right after a successful install. */
function InstallDialog({
  item,
  agentOptions,
  isUpdate,
  onCancel,
  onDone,
}: {
  item: StoreItem
  agentOptions: { key: string; label: string; hint: string }[]
  isUpdate: boolean
  onCancel: () => void
  onDone: (msg: { kind: 'ok' | 'err'; msg: string }) => void
}) {
  const fields = item.config_schema ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const [targets, setTargets] = useState<string[]>(isUpdate ? item.mounted ?? [] : [])
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    setSaving(true)
    const r = await installStoreItem(item.type, item.id, values)
    if (!r) {
      setSaving(false)
      setErr('安装失败（serve 未连接）')
      return
    }
    if (!r.ok) {
      setSaving(false)
      setErr(r.error ?? '安装失败')
      return
    }
    let msg = isUpdate ? `已更新 ${item.title}` : `已安装 ${item.title}`
    if (item.type === 'mcp') {
      msg += '；连接在后台进行，稍后看「已安装」tab 的状态'
    } else {
      msg += '；skills_list 立即可用，新会话的技能索引可见'
    }
    if (targets.length) {
      const m = await mountStoreItem(item.type, item.id, targets)
      setSaving(false)
      if (!m?.ok) {
        onDone({ kind: 'err', msg: `${msg}；但挂载失败：${m?.error ?? 'serve 未连接'}` })
        return
      }
      const needRestart = (m.mounted ?? []).includes('main')
      msg += needRestart
        ? '；挂载已保存（主 agent 需重启 serve 生效，specialist 下次调用即生效）'
        : '；挂载已保存（specialist 下次调用即生效）'
    } else {
      setSaving(false)
    }
    onDone({ kind: 'ok', msg })
  }

  return (
    <Overlay
      title={isUpdate ? (item.upgradable ? `更新：${item.title}` : `重装：${item.title}`) : `安装：${item.title}`}
      onClose={onCancel}
    >
      <div className="space-y-3">
        {item.description && (
          <p className="text-xs leading-relaxed text-ink-3">{item.description}</p>
        )}

        {item.type === 'mcp' && fields.length > 0 && (
          <div className="space-y-2">
            {fields.map((f) => (
              <label key={f.key} className="block">
                <span className="mb-1 flex items-center gap-1 text-xs text-ink-2">
                  {f.label || f.key}
                  {f.required && <span className="text-danger">*</span>}
                  {isUpdate && item.secrets_set?.[f.key] && (
                    <span className="text-[10px] text-ink-4">（已保存，留空保持）</span>
                  )}
                </span>
                <input
                  type={f.type === 'password' ? 'password' : 'text'}
                  value={values[f.key] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                  autoComplete="off"
                  className="w-full rounded-lg border border-line-strong bg-app px-2.5 py-1.5 text-xs text-ink outline-none focus:border-sky-700"
                />
              </label>
            ))}
          </div>
        )}

        <MountCheckboxes
          agentOptions={agentOptions}
          targets={targets}
          onChange={setTargets}
          note={
            item.type === 'mcp'
              ? '主 agent 名单含 mcp 时装完即可用；挂载主要让 specialist 获得 mcp-<id> 名单'
              : undefined
          }
        />

        {err && (
          <div className="rounded-lg border border-danger/50 px-3 py-1.5 text-[11px] text-danger/80">
            {err}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-2 transition hover:bg-elevated"
          >
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={saving}
            className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent transition hover:bg-accent/20 disabled:opacity-40"
          >
            {saving ? '处理中…' : isUpdate ? '更新' : '安装'}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

/** Standalone mount editor for an installed item (replace semantics). */
function MountDialog({
  item,
  agentOptions,
  onCancel,
  onDone,
}: {
  item: StoreItem
  agentOptions: { key: string; label: string; hint: string }[]
  onCancel: () => void
  onDone: (msg: { kind: 'ok' | 'err'; msg: string }) => void
}) {
  const [targets, setTargets] = useState<string[]>(item.mounted ?? [])
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    setSaving(true)
    const r = await mountStoreItem(item.type, item.id, targets)
    setSaving(false)
    if (!r) {
      onDone({ kind: 'err', msg: '挂载失败（serve 未连接）' })
      return
    }
    if (!r.ok) {
      onDone({ kind: 'err', msg: r.error ?? '挂载失败' })
      return
    }
    const needRestart = (r.mounted ?? []).includes('main')
    onDone({
      kind: 'ok',
      msg: needRestart
        ? `挂载已保存：主 agent 需重启 serve 生效，specialist 下次调用即生效`
        : '挂载已保存：specialist 下次调用即生效',
    })
  }

  return (
    <Overlay title={`挂载：${item.title}`} onClose={onCancel}>
      <div className="space-y-3">
        <MountCheckboxes agentOptions={agentOptions} targets={targets} onChange={setTargets} />
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-2 transition hover:bg-elevated"
          >
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={saving}
            className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent transition hover:bg-accent/20 disabled:opacity-40"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function MountCheckboxes({
  agentOptions,
  targets,
  onChange,
  note,
}: {
  agentOptions: { key: string; label: string; hint: string }[]
  targets: string[]
  onChange: (next: string[]) => void
  note?: string
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs text-ink-2">挂载给谁用</div>
      {note && <div className="text-[10px] text-ink-4">{note}</div>}
      {agentOptions.map((a) => (
        <label
          key={a.key}
          className="flex cursor-pointer items-center gap-2 rounded-lg border border-line px-2.5 py-1.5 transition hover:bg-elevated/60"
        >
          <input
            type="checkbox"
            checked={targets.includes(a.key)}
            onChange={(e) =>
              onChange(
                e.target.checked ? [...targets, a.key] : targets.filter((t) => t !== a.key)
              )
            }
            className="h-3.5 w-3.5 accent-sky-500"
          />
          <span className="text-xs text-ink">{a.label}</span>
          <span className="ml-auto text-[10px] text-ink-4">{a.hint}</span>
        </label>
      ))}
    </div>
  )
}

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
  tone?: 'neutral' | 'sky' | 'emerald' | 'rose' | 'amber'
}) {
  const cls = {
    neutral: 'border-line-strong text-ink-3',
    sky: 'border-accent/50 text-accent',
    emerald: 'border-success/50 text-success',
    rose: 'border-danger/50 text-danger',
    amber: 'border-warning/50 text-warning',
  }[tone]
  return <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', cls)}>{children}</span>
}
