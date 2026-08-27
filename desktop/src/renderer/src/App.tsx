import { useEffect, useState } from 'react'
import { Check, ChevronDown, FolderTree, Monitor, Settings, ShoppingBag, X } from 'lucide-react'
import { useStore } from './store'
import { Sidebar } from './components/Sidebar'
import { ChatPanel } from './components/ChatPanel'
import { SettingsPanel } from './components/SettingsPanel'
import { StorePage } from './components/StorePage'
import { FileTreePanel } from './components/FileTreePanel'
import { BrowserPanel } from './components/BrowserPanel'
import { cn } from './lib/cn'
import { desktop } from './lib/desktop'

export default function App() {
  const agent = useStore((s) => s.agents.find((a) => a.id === s.selectedAgentId) ?? null)
  const connectServe = useStore((s) => s.connectServe)
  const hydrateWorkspaceStore = useStore((s) => s.hydrateWorkspaceStore)
  const hydrateXiheConfig = useStore((s) => s.hydrateXiheConfig)
  const activeTab = useStore((s) => s.activeTab)
  const setTab = useStore((s) => s.setTab)
  const convWorkspace = useStore((s) => s.convWorkspace)
  const workspaces = useStore((s) => s.workspaces)
  const bindWorkspaceToConv = useStore((s) => s.bindWorkspaceToConv)
  const [showTree, setShowTree] = useState(false)
  const showBrowser = useStore((s) => s.showBrowser)
  const setShowBrowser = useStore((s) => s.setShowBrowser)
  const [chipOpen, setChipOpen] = useState(false)

  // Fallback: if main's xihe:status push never arrives, this /health probe still connects.
  useEffect(() => {
    void connectServe()
  }, [connectServe])

  // xihe:status — pull a snapshot on mount (an early `running` push can be
  // dropped before did-finish-load), then subscribe to pushes.
  const applyXiheStatus = useStore((s) => s.applyXiheStatus)
  useEffect(() => {
    let alive = true
    void desktop.getXiheStatus().then((s) => {
      if (alive && s) applyXiheStatus(s)
    })
    const off = desktop.onXiheStatus(applyXiheStatus)
    return () => {
      alive = false
      off()
    }
  }, [applyXiheStatus])

  useEffect(() => {
    void hydrateWorkspaceStore()
  }, [hydrateWorkspaceStore])

  useEffect(() => {
    void hydrateXiheConfig()
  }, [hydrateXiheConfig])

  const theme = useStore((s) => s.theme)
  const hydrateTheme = useStore((s) => s.hydrateTheme)
  useEffect(() => {
    void hydrateTheme()
  }, [hydrateTheme])

  // <html data-theme> drives every semantic color token (index.css — one
  // block per appearance). system mode follows the OS: Electron mirrors
  // nativeTheme into prefers-color-scheme, so the media listener re-resolves
  // the attribute without a store change.
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => {
      const dark = theme === 'dark' || (theme === 'system' && mq.matches)
      document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    }
    apply()
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [theme])

  // Workspace bound to the active conversation. Derived here (not stored on
  // ConvMeta) so it survives syncConversations rebuilds — the binding lives
  // only in the convWorkspace map.
  const activeConvId = agent?.activeConvId
  const boundWsId = activeConvId ? convWorkspace[activeConvId] : undefined
  const activeWs = boundWsId ? workspaces.find((w) => w.id === boundWsId) : undefined
  const dismissBrowserPanel = useStore((s) => s.dismissBrowserPanel)

  useEffect(() => {
    if (activeWs) setShowTree(true)
  }, [activeWs?.id])

  // The browser panel belongs to the conversation that opened it — switching
  // conversations hides the Chrome first (main), then closes the panel without
  // muting auto-open, so a browser tool in the new conversation still pops it.
  // Guarded on showBrowser: at boot (and whenever the panel is closed) this
  // effect's first run must not fire a hide at a serve that may not be up yet.
  // showBrowser is read as a snapshot on purpose — not a dep.
  useEffect(() => {
    if (!showBrowser) return
    void desktop.setBrowserPanelActive(false)
    dismissBrowserPanel()
  }, [activeConvId, dismissBrowserPanel])

  // Settings and the store are full-window pages (own Header + back arrow).
  // The chat layout stays MOUNTED underneath — unmounting it would tear down
  // the browser panel and release the snapped Chrome; instead it hides, and
  // the panel drives Chrome hide/show through its `active` prop.
  return (
    <>
      <div className={cn('flex h-screen bg-app text-ink', activeTab !== 'chat' && 'hidden')}>
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        {agent ? (
          <>
            <header className="flex items-center gap-3 border-b border-line px-5 py-3">
              <h1 className="text-sm font-semibold">{agent.name}</h1>
              <span className="text-xs text-ink-4">{agent.model}</span>
              <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-ink-3">
                {agent.shape === 'process' ? '进程型' : 'connector 型'}
              </span>

              {/* Workspace chip + tree toggle (advanced). */}
              {activeConvId && (
                <div className="relative flex items-center gap-1">
                  <button
                    onClick={() => setChipOpen((v) => !v)}
                    className="flex items-center gap-1 rounded bg-elevated px-2 py-1 text-xs text-ink-2 transition hover:bg-strong"
                    title="工作空间"
                  >
                    <FolderTree className="h-3.5 w-3.5 text-accent" />
                    <span className="max-w-[10rem] truncate">{activeWs ? activeWs.name : '通用'}</span>
                    <ChevronDown className="h-3 w-3 opacity-70" />
                  </button>
                  {chipOpen && (
                    <>
                      {/* Click-away backdrop (zero-dep popover close). */}
                      <button
                        className="fixed inset-0 z-10 cursor-default"
                        onClick={() => setChipOpen(false)}
                        aria-hidden
                      />
                      <div className="absolute right-0 top-full z-20 mt-1 min-w-[12rem] overflow-hidden rounded-lg border border-line-strong bg-panel py-1 shadow-xl">
                        <div className="px-2 pb-1 pt-1 text-[10px] uppercase tracking-wider text-ink-4">
                          绑定工作空间
                        </div>
                        {workspaces.length === 0 && (
                          <div className="px-3 py-1.5 text-xs text-ink-4">
                            无工作空间，先在侧栏添加
                          </div>
                        )}
                        {workspaces.map((w) => (
                          <button
                            key={w.id}
                            onClick={() => {
                              bindWorkspaceToConv(activeConvId, w.id)
                              setChipOpen(false)
                            }}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-ink-2 transition hover:bg-elevated"
                          >
                            <span className="flex-1 truncate">{w.name}</span>
                            {activeWs?.id === w.id && <Check className="h-3 w-3 text-accent" />}
                          </button>
                        ))}
                        <div className="my-1 border-t border-line" />
                        <button
                          onClick={() => {
                            bindWorkspaceToConv(activeConvId, null)
                            setChipOpen(false)
                          }}
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-ink-3 transition hover:bg-elevated"
                        >
                          <X className="h-3 w-3" />
                          通用（无工作空间）
                        </button>
                      </div>
                    </>
                  )}
                  <button
                    onClick={() => setShowTree((v) => !v)}
                    disabled={!activeWs}
                    title={activeWs ? '显示/隐藏文件树' : '先绑定工作空间'}
                    className={cn(
                      'rounded p-1.5 transition',
                      activeWs && showTree
                        ? 'bg-contrast text-sky-300'
                        : 'text-ink-3 hover:bg-elevated',
                      !activeWs && 'cursor-not-allowed opacity-40'
                    )}
                  >
                    <FolderTree className="h-4 w-4" />
                  </button>
                </div>
              )}

              <div className="ml-auto flex items-center gap-1">
                <button
                  onClick={() => setShowBrowser(!showBrowser)}
                  title="浏览器面板（agent 的 Chrome 吸附于此）"
                  className={cn(
                    'rounded-lg p-1.5 transition',
                    showBrowser
                      ? 'bg-contrast text-sky-300'
                      : 'text-ink-3 hover:bg-elevated hover:text-ink'
                  )}
                >
                  <Monitor className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setTab('store')}
                  title="商店"
                  className="rounded-lg p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
                >
                  <ShoppingBag className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setTab('manage')}
                  title="设置"
                  className="rounded-lg p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
                >
                  <Settings className="h-4 w-4" />
                </button>
              </div>
            </header>
            <div className="min-h-0 flex-1">
              <div className="flex h-full">
                <div className="min-w-0 flex-1">
                  <ChatPanel agent={agent} />
                </div>
                {activeWs && showTree && (
                  <FileTreePanel
                    workspace={activeWs}
                    className="w-80 shrink-0 border-l border-line"
                  />
                )}
                {showBrowser && (
                  <BrowserPanel
                    active={activeTab === 'chat'}
                    className="shrink-0 border-l border-line"
                  />
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-ink-4">
            选择左侧的 agent 开始
          </div>
        )}
      </main>
      </div>
      {activeTab === 'manage' && agent && (
        <SettingsPanel agent={agent} onBack={() => setTab('chat')} />
      )}
      {activeTab === 'store' && agent && <StorePage onBack={() => setTab('chat')} />}
    </>
  )
}
