import { useEffect, useRef, useState } from 'react'
import {
  ChevronLeft,
  Folder,
  FolderPlus,
  MessageSquare,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  Wifi,
  WifiOff,
} from 'lucide-react'
import { useStore, type ConvMeta } from '../store'
import { cn } from '../lib/cn'
import { desktop } from '../lib/desktop'

export function Sidebar() {
  const agents = useStore((s) => s.agents)
  const selectedId = useStore((s) => s.selectedAgentId)
  const serveConnected = useStore((s) => s.serveConnected)
  const serveVersion = useStore((s) => s.serveVersion)
  const xiheStatus = useStore((s) => s.xiheStatus)
  const newConversation = useStore((s) => s.newConversation)
  const selectConversation = useStore((s) => s.selectConversation)
  const deleteConversation = useStore((s) => s.deleteConversation)
  const refreshConversation = useStore((s) => s.refreshConversation)
  const renameConversation = useStore((s) => s.renameConversation)
  const workspaces = useStore((s) => s.workspaces)
  const addWorkspace = useStore((s) => s.addWorkspace)
  const removeWorkspace = useStore((s) => s.removeWorkspace)
  const openConversationInWorkspace = useStore((s) => s.openConversationInWorkspace)
  const openWorkspace = useStore((s) => s.openWorkspace)
  const exitWorkspace = useStore((s) => s.exitWorkspace)
  const activeWorkspaceId = useStore((s) => s.activeWorkspaceId)
  const convWorkspace = useStore((s) => s.convWorkspace)
  // Which rail column is expanded. 'convs' is the default view (mirrors the
  // pre-rail layout); 'ws' swaps in the workspace list. In a workspace view
  // the header collapses to a back arrow + name, as before.
  const [railTab, setRailTab] = useState<'convs' | 'ws'>('convs')

  // In workspace view the sidebar scopes to one workspace's conversations.
  // Those convs live under the selected agent (xihe); convWorkspace is what
  // filters them down to this workspace.
  const activeWsObj = activeWorkspaceId
    ? workspaces.find((w) => w.id === activeWorkspaceId)
    : undefined
  const wsAgent = activeWorkspaceId ? agents.find((a) => a.id === selectedId) : undefined
  const boundConvs =
    activeWorkspaceId && wsAgent
      ? wsAgent.conversations.filter((c) => convWorkspace[c.id] === activeWorkspaceId)
      : []
  // Single built-in agent (xihe) — its conversations are shown directly at the
  // top, no agent grouping/selection layer.
  const selAgent = agents.find((a) => a.id === selectedId) ?? agents[0]

  return (
    <aside className="flex border-r border-line bg-panel/50">
      {/* Icon rail — the two navigation roots. 对话 is the default column. */}
      <div className="flex w-12 shrink-0 flex-col items-center gap-1 border-r border-line py-3">
        <button
          onClick={() => setRailTab('convs')}
          title="对话"
          className={cn(
            'flex h-9 w-9 items-center justify-center rounded-lg transition',
            railTab === 'convs' && !activeWorkspaceId
              ? 'bg-brand/20 text-brand'
              : 'text-ink-4 hover:bg-elevated hover:text-ink-2'
          )}
        >
          <MessageSquare className="h-4 w-4" />
        </button>
        <button
          onClick={() => setRailTab('ws')}
          title="工作空间"
          className={cn(
            'flex h-9 w-9 items-center justify-center rounded-lg transition',
            railTab === 'ws' || activeWorkspaceId
              ? 'bg-accent/20 text-accent'
              : 'text-ink-4 hover:bg-elevated hover:text-ink-2'
          )}
        >
          <Folder className="h-4 w-4" />
        </button>
      </div>

      {/* Content column — one of the two roots, or the in-workspace view. */}
      <div className="flex w-52 flex-col">
      <div className="px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-brand text-xs font-bold text-white">
            m
          </div>
          <div>
            <div className="text-xs font-semibold">xihe desktop</div>
            <div className="text-[9px] text-ink-4">control plane · v0.0.1</div>
          </div>
        </div>
      </div>

      {activeWorkspaceId ? (
        <div className="flex items-center gap-1 px-2 pb-1 pt-2">
          <button
            onClick={exitWorkspace}
            title="退出工作空间"
            className="rounded p-0.5 text-ink-4 transition hover:bg-elevated hover:text-ink"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <span className="flex-1 truncate text-[10px] font-semibold uppercase tracking-wider text-ink-4">
            {activeWsObj?.name ?? '工作空间'}
          </span>
        </div>
      ) : (
        <div className="px-4 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-4">
          {railTab === 'ws' ? '工作空间' : '对话'}
        </div>
      )}
      <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
        {activeWorkspaceId ? (
          // convWorkspace binding scopes this list to the workspace; new convs
          // started here are auto-bound to it.
          <>
            <button
              onClick={() => openConversationInWorkspace(activeWorkspaceId)}
              className="flex w-full items-center gap-1.5 rounded-md border border-dashed border-line-strong/70 px-2.5 py-1.5 text-left text-xs text-ink-4 transition hover:border-ink-4 hover:text-ink-2"
            >
              <Plus className="h-3 w-3" /> 新对话
            </button>
            {boundConvs.map((c) => (
              <ConversationRow
                key={c.id}
                conv={c}
                active={!!wsAgent && c.id === wsAgent.activeConvId}
                onSelect={() => wsAgent && selectConversation(wsAgent.id, c.id)}
                onRename={(t) => wsAgent && void renameConversation(wsAgent.id, c.id, t)}
                onRefresh={() => wsAgent && void refreshConversation(wsAgent.id, c.id)}
                onDelete={() => {
                  if (wsAgent) void deleteConversation(wsAgent.id, c.id)
                }}
              />
            ))}
            {boundConvs.length === 0 && (
              <div className="px-2.5 py-1.5 text-xs text-ink-4">暂无会话</div>
            )}
          </>
        ) : railTab === 'ws' ? (
          <>
            {workspaces.map((w) => (
              <div key={w.id} className="group relative">
                <button
                  onClick={() => openWorkspace(w.id)}
                  title={w.workdir}
                  className="flex w-full flex-col gap-1 rounded-lg px-2.5 py-2 text-left transition hover:bg-elevated/50"
                >
                  <div className="flex items-center gap-2">
                    <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
                    <span className="truncate text-sm">{w.name}</span>
                  </div>
                  <span className="ml-5 truncate rounded bg-accent/10 px-1 text-[10px] text-accent">
                    {w.workdir}
                  </span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    if (window.confirm(`删除工作空间「${w.name}」？绑定的会话将变为通用。`))
                      removeWorkspace(w.id)
                  }}
                  title="删除工作空间"
                  className="absolute right-1 top-2 rounded p-0.5 text-ink-4 opacity-0 transition hover:text-danger group-hover:opacity-100"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
            <button
              onClick={async () => {
                const dir = await desktop.openDirectory()
                if (!dir) return // user cancelled the picker
                // window.prompt is unsupported in Electron (returns null) — name defaults to the basename.
                addWorkspace(dir)
              }}
              className="flex w-full items-center gap-1.5 rounded-md border border-dashed border-line-strong/70 px-2.5 py-1.5 text-left text-xs text-ink-4 transition hover:border-ink-4 hover:text-ink-2"
            >
              <FolderPlus className="h-3 w-3" /> 添加工作空间
            </button>
            {workspaces.length === 0 && (
              <div className="px-2.5 py-1.5 text-xs text-ink-4">暂无工作空间</div>
            )}
          </>
        ) : (
          <>
            {selAgent && (
              <>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => newConversation(selAgent.id)}
                    className="flex flex-1 items-center gap-1.5 rounded-md border border-dashed border-line-strong/70 px-2.5 py-1.5 text-left text-xs text-ink-4 transition hover:border-ink-4 hover:text-ink-2"
                  >
                    <Plus className="h-3 w-3" /> 新对话
                  </button>
                  {selAgent.serveBacked && (
                    <button
                      onClick={() => void refreshConversation(selAgent.id)}
                      title="从xihe重新拉取会话列表"
                      className="rounded-md p-1.5 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
                    >
                      <RefreshCw className="h-3 w-3" />
                    </button>
                  )}
                </div>
                {selAgent.conversations.map((c) => (
                  <ConversationRow
                    key={c.id}
                    conv={c}
                    active={c.id === selAgent.activeConvId}
                    onSelect={() => selectConversation(selAgent.id, c.id)}
                    onRename={(t) => void renameConversation(selAgent.id, c.id, t)}
                    onRefresh={
                      selAgent.serveBacked
                        ? () => void refreshConversation(selAgent.id, c.id)
                        : undefined
                    }
                    refreshTitle="从xihe同步该会话历史"
                    onDelete={() => void deleteConversation(selAgent.id, c.id)}
                  />
                ))}
                {selAgent.conversations.length === 0 && (
                  <div className="px-2.5 py-1.5 text-xs text-ink-4">暂无会话</div>
                )}
              </>
            )}
          </>
        )}
      </nav>

      <div className="space-y-2 border-t border-line p-3">
        <div
          className={cn(
            'flex items-center gap-1.5 text-[11px]',
            serveConnected
              ? 'text-success/90'
              : xiheStatus?.state === 'not_found'
                ? 'text-danger/90'
                : 'text-warning/80'
          )}
          title={xiheStatus?.state === 'not_found' ? xiheStatus.message : undefined}
        >
          {serveConnected ? (
            <>
              <Wifi className="h-3 w-3" /> xihe运行中{serveVersion ? ` · v${serveVersion}` : ''}
            </>
          ) : xiheStatus?.state === 'not_found' ? (
            <>
              <WifiOff className="h-3 w-3" /> xihe未安装 — pip install -e .
            </>
          ) : (
            <>
              <WifiOff className="h-3 w-3" />{' '}
              {xiheStatus?.state === 'errored'
                ? xiheStatus.message ?? 'xihe未运行'
                : xiheStatus?.state === 'stopped'
                  ? 'xihe未运行'
                  : 'xihe启动中…'}
            </>
          )}
        </div>
      </div>
      </div>
    </aside>
  )
}

/** A single conversation row in the sidebar: the title (click to open), with a
 *  hover toolbar of rename / sync / delete. Rename swaps the title for an inline
 *  input — Enter or blur commits, Escape cancels. Extracted so the workspace
 *  view and the agent-centric view share one implementation. */
function ConversationRow({
  conv,
  active,
  onSelect,
  onRename,
  onRefresh,
  onDelete,
  refreshTitle,
}: {
  conv: ConvMeta
  active: boolean
  onSelect: () => void
  onRename: (title: string) => void
  onRefresh?: () => void
  onDelete: () => void
  refreshTitle?: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(conv.title)
  const inputRef = useRef<HTMLInputElement>(null)
  // Two-step delete instead of window.confirm: the native dialog's focus
  // restore lands on the row being unmounted, so focus drops to <body> and
  // the composer then swallows nothing until the user clicks somewhere.
  const [confirmDel, setConfirmDel] = useState(false)
  const disarmRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (disarmRef.current) clearTimeout(disarmRef.current)
    },
    []
  )

  // Focus + select-all on enter so continued typing replaces the old title.
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editing])
  // Mirror outside title changes (e.g. a serve-generated title landing via
  // sync) into the draft while NOT editing, so reopening the editor shows the
  // current title.
  useEffect(() => {
    if (!editing) setDraft(conv.title)
  }, [conv.title, editing])

  const commit = () => {
    const t = draft.trim()
    setEditing(false)
    if (!t || t === conv.title) return
    onRename(t)
  }

  return (
    <div className="group relative">
      {editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              commit()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              setEditing(false)
            }
          }}
          onBlur={commit}
          className="w-full rounded-md border border-brand/60 bg-panel px-2 py-1.5 text-xs text-ink outline-none"
        />
      ) : (
        <button
          onClick={onSelect}
          className={cn(
            'flex w-full items-center rounded-md px-2.5 py-1.5 text-left text-xs transition',
            active
              ? 'bg-elevated text-ink'
              : 'text-ink-3 hover:bg-elevated/50 hover:text-ink'
          )}
        >
          <span className="truncate pr-12">{conv.title}</span>
        </button>
      )}
      {!editing && (
        <div className="absolute right-1 top-1/2 flex -translate-y-1/2 gap-0.5 opacity-0 transition group-hover:opacity-100">
          <button
            onClick={(e) => {
              e.stopPropagation()
              setDraft(conv.title)
              setEditing(true)
            }}
            title="重命名会话"
            className="rounded p-0.5 text-ink-4 transition hover:text-success"
          >
            <Pencil className="h-3 w-3" />
          </button>
          {onRefresh && (
            <button
              onClick={(e) => {
                e.stopPropagation()
                onRefresh()
              }}
              title={refreshTitle ?? '从xihe同步该会话历史'}
              className="rounded p-0.5 text-ink-4 transition hover:text-accent"
            >
              <RefreshCw className="h-3 w-3" />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation()
              if (disarmRef.current) clearTimeout(disarmRef.current)
              if (!confirmDel) {
                setConfirmDel(true)
                disarmRef.current = setTimeout(() => setConfirmDel(false), 3000)
                return
              }
              setConfirmDel(false)
              onDelete()
            }}
            title={confirmDel ? '再点一次确认删除' : '删除会话'}
            className={cn(
              'rounded p-0.5 text-[10px] transition',
              confirmDel ? 'text-danger' : 'text-ink-4 hover:text-danger'
            )}
          >
            {confirmDel ? '确认?' : <Trash2 className="h-3 w-3" />}
          </button>
        </div>
      )}
    </div>
  )
}
