import { useEffect, useRef, useState, type ReactNode } from 'react'
import { BookOpen, CodeXml, FolderTree, Globe, MessageSquare, PanelLeftOpen, PanelRightClose, PanelRightOpen, Play, Settings, ShoppingBag, SquareTerminal } from 'lucide-react'
import { useStore } from './store'
import { Sidebar } from './components/Sidebar'
import { ChatPanel } from './components/ChatPanel'
import { SettingsPanel } from './components/SettingsPanel'
import { StorePage } from './components/StorePage'
import { KnowledgePage } from './components/KnowledgePage'
import { FileTree, FileTreePanel } from './components/FileTreePanel'
import { EditorArea } from './components/EditorArea'
import { BrowserPanel } from './components/BrowserPanel'
import { TerminalPanel } from './components/TerminalPanel'
import { RunPanel } from './components/RunPanel'
import { Resizer, usePanelSize } from './components/Resizer'
import { cn } from './lib/cn'
import { desktop } from './lib/desktop'

/** Re-opener strip left in place of a collapsed panel — keeps the restore
 *  affordance at the edge the panel occupied. */
function EdgeStrip({ side, title, onClick, children }: {
  side: 'left' | 'right'
  title: string
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={cn(
        'flex w-9 shrink-0 flex-col items-center gap-2 pt-3 text-ink-4 transition hover:bg-elevated hover:text-ink',
        side === 'left' ? 'border-r border-line' : 'border-l border-line'
      )}
    >
      {children}
    </button>
  )
}

export default function App() {
  const agent = useStore((s) => s.agents.find((a) => a.id === s.selectedAgentId) ?? null)
  const connectServe = useStore((s) => s.connectServe)
  const hydrateWorkspaceStore = useStore((s) => s.hydrateWorkspaceStore)
  const hydrateXiheConfig = useStore((s) => s.hydrateXiheConfig)
  const activeTab = useStore((s) => s.activeTab)
  const setTab = useStore((s) => s.setTab)
  const convWorkspace = useStore((s) => s.convWorkspace)
  const workspaces = useStore((s) => s.workspaces)
  // Panel-collapse states (session-local, like showBrowser): the panel folds
  // to an EdgeStrip instead of vanishing, so restore is in place.
  const [treeCollapsed, setTreeCollapsed] = useState(false)
  const [rightTreeCollapsed, setRightTreeCollapsed] = useState(false)
  const [chatCollapsed, setChatCollapsed] = useState(false)
  const showBrowser = useStore((s) => s.showBrowser)
  const setShowBrowser = useStore((s) => s.setShowBrowser)
  const showTerminal = useStore((s) => s.showTerminal)
  const setShowTerminal = useStore((s) => s.setShowTerminal)
  const showRun = useStore((s) => s.showRun)
  const setShowRun = useStore((s) => s.setShowRun)
  const openRunPanel = useStore((s) => s.openRunPanel)
  const layoutMode = useStore((s) => s.layoutMode)
  const setLayoutMode = useStore((s) => s.setLayoutMode)
  const editorTabs = useStore((s) => s.editorTabs)
  const activeEditorTabId = useStore((s) => s.activeTabId)
  const openFileTab = useStore((s) => s.openFileTab)
  const closeTabsUnder = useStore((s) => s.closeTabsUnder)
  const renameTabPath = useStore((s) => s.renameTabPath)
  // Workbench chat column width (drag its left edge). The trees and drawers
  // own their own sizes internally (FileTree / TerminalPanel / RunPanel).
  const [chatW, setChatW] = usePanelSize('chatPanelWidth', 460, 360)
  const chatColRef = useRef<HTMLDivElement>(null)
  // A turn is streaming in the active conversation — cues the collapsed
  // workbench chat strip so replies aren't silently missed.
  const chatBusy = useStore((s) => {
    const convId = s.agents.find((a) => a.id === s.selectedAgentId)?.activeConvId
    return !!convId && (s.sessions[convId] ?? []).some((m) => m.role === 'assistant' && m.pending)
  })

  // Path of the active FILE tab (drives the tree's selected highlight in
  // workbench mode). Diff tabs don't highlight anything.
  const activeEditorTab = editorTabs.find((t) => t.id === activeEditorTabId) ?? null
  const activeEditorPath =
    activeEditorTab?.kind === 'file' && activeEditorTab.path ? activeEditorTab.path : null

  // Fallback: if main's xihe:status push never arrives, this /health probe still connects.
  useEffect(() => {
    void connectServe()
  }, [connectServe])

  // Dev only: monaco sits outside the dep optimizer (worker imports), so its
  // first load streams hundreds of source modules. Warm it shortly after
  // startup so the FIRST file open doesn't pay that. Prod keeps the lazy
  // chunk — nothing loads until an editor is actually opened.
  useEffect(() => {
    if (!import.meta.env.DEV) return
    const t = setTimeout(() => {
      void import('./components/monaco/env').then((m) => m.ensureMonaco())
    }, 1500)
    return () => clearTimeout(t)
  }, [])

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
  // Workbench needs a bound workspace (its tree/editor act on it). Without a
  // binding the chat layout renders even while the preference says workbench,
  // and the layout toggle itself is hidden.
  const inWorkbench = !!activeWs && layoutMode === 'workbench'
  const dismissBrowserPanel = useStore((s) => s.dismissBrowserPanel)

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
              <div className="ml-auto flex items-center gap-1">
                {activeWs && (
                  <button
                    onClick={() => void setLayoutMode(inWorkbench ? 'chat' : 'workbench')}
                    title="工作台布局（文件树 + 编辑器为中心）"
                    className={cn(
                      'rounded-lg p-1.5 transition',
                      inWorkbench
                        ? 'bg-contrast text-sky-300'
                        : 'text-ink-3 hover:bg-elevated hover:text-ink'
                    )}
                  >
                    <CodeXml className="h-4 w-4" />
                  </button>
                )}
                <button
                  onClick={() =>
                    inWorkbench
                      ? setTreeCollapsed((v) => !v)
                      : setRightTreeCollapsed((v) => !v)
                  }
                  disabled={!activeWs}
                  title={activeWs ? '显示/隐藏文件树' : '先绑定工作空间'}
                  className={cn(
                    'rounded-lg p-1.5 transition',
                    activeWs && (inWorkbench ? !treeCollapsed : !rightTreeCollapsed)
                      ? 'bg-contrast text-sky-300'
                      : 'text-ink-3 hover:bg-elevated hover:text-ink',
                    !activeWs && 'cursor-not-allowed opacity-40'
                  )}
                >
                  <FolderTree className="h-4 w-4" />
                </button>
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
                  <Globe className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setShowRun(!showRun)}
                  // Run executes with a workspace cwd — without a binding the
                  // panel is inert. Still clickable while open so it can close.
                  disabled={!activeWs && !showRun}
                  title={activeWs ? '运行面板（在工作空间执行一次性命令，带历史）' : '先绑定工作空间'}
                  className={cn(
                    'rounded-lg p-1.5 transition',
                    showRun
                      ? 'bg-contrast text-sky-300'
                      : 'text-ink-3 hover:bg-elevated hover:text-ink',
                    !activeWs && !showRun && 'cursor-not-allowed opacity-40'
                  )}
                >
                  <Play className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setShowTerminal(!showTerminal)}
                  title="终端面板（本地 shell / agent 的本地命令 / SSH 会话）"
                  className={cn(
                    'rounded-lg p-1.5 transition',
                    showTerminal
                      ? 'bg-contrast text-sky-300'
                      : 'text-ink-3 hover:bg-elevated hover:text-ink'
                  )}
                >
                  <SquareTerminal className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setTab('store')}
                  title="商店"
                  className="rounded-lg p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
                >
                  <ShoppingBag className="h-4 w-4" />
                </button>
                <button
                  onClick={() => setTab('knowledge')}
                  title="记忆 / 知识库"
                  className="rounded-lg p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
                >
                  <BookOpen className="h-4 w-4" />
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
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="flex h-full min-h-0 flex-1">
                {inWorkbench ? (
                  <>
                    {treeCollapsed ? (
                      <EdgeStrip
                        side="left"
                        title="展开文件树"
                        onClick={() => setTreeCollapsed(false)}
                      >
                        <PanelLeftOpen className="h-4 w-4" />
                        <FolderTree className="h-3.5 w-3.5" />
                      </EdgeStrip>
                    ) : (
                      <FileTree
                        workspace={activeWs}
                        className="shrink-0 border-r border-line"
                        selectedPath={activeEditorPath}
                        side="left"
                        onCollapse={() => setTreeCollapsed(true)}
                        onRunFile={(c) => openRunPanel(c)}
                        onSelectFile={(e) => openFileTab(e.path)}
                        onPathRenamed={renameTabPath}
                        onPathDeleted={closeTabsUnder}
                      />
                    )}
                    <EditorArea className="min-w-0 flex-1" />
                    {chatCollapsed ? (
                      <EdgeStrip
                        side="right"
                        title="展开对话"
                        onClick={() => setChatCollapsed(false)}
                      >
                        <PanelRightOpen className="h-4 w-4" />
                        <MessageSquare className="h-3.5 w-3.5" />
                        {chatBusy && (
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                        )}
                      </EdgeStrip>
                    ) : (
                      <div
                        ref={chatColRef}
                        className="relative shrink-0 border-l border-line"
                        style={{ width: chatW }}
                      >
                        <Resizer
                          axis="col"
                          title="拖动调整面板宽度"
                          onMove={(x) => {
                            const el = chatColRef.current
                            if (!el) return
                            const w = el.getBoundingClientRect().right - x
                            const max = Math.max(window.innerWidth - 420, 360)
                            setChatW(Math.min(Math.max(w, 360), max))
                          }}
                          className="absolute left-0 top-0 h-full w-1.5 -translate-x-1/2"
                        />
                        <button
                          onClick={() => setChatCollapsed(true)}
                          title="折叠对话面板"
                          className="absolute left-0 top-2 z-10 rounded p-1 text-ink-4 opacity-50 transition hover:bg-elevated hover:text-ink hover:opacity-100"
                        >
                          <PanelRightClose className="h-3.5 w-3.5" />
                        </button>
                        <ChatPanel agent={agent} />
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <div className="min-w-0 flex-1">
                      <ChatPanel agent={agent} />
                    </div>
                    {activeWs && !rightTreeCollapsed && (
                      <FileTreePanel
                        workspace={activeWs}
                        className="shrink-0 border-l border-line"
                        onCollapse={() => setRightTreeCollapsed(true)}
                        onRunFile={(c) => openRunPanel(c)}
                      />
                    )}
                    {activeWs && rightTreeCollapsed && (
                      <EdgeStrip
                        side="right"
                        title="展开文件树"
                        onClick={() => setRightTreeCollapsed(false)}
                      >
                        <PanelRightOpen className="h-4 w-4" />
                        <FolderTree className="h-3.5 w-3.5" />
                      </EdgeStrip>
                    )}
                  </>
                )}
                {showBrowser && (
                  <BrowserPanel
                    active={activeTab === 'chat'}
                    className="shrink-0 border-l border-line"
                  />
                )}
              </div>
              {/* The terminal drawer spans the full content width — sessions
                  are cross-conversation (serve-process globals), unlike the
                  browser panel which belongs to the active conversation. */}
              {showTerminal && (
                <TerminalPanel className="shrink-0 border-t border-line" />
              )}
              {showRun && <RunPanel className="shrink-0 border-t border-line" />}
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
      {activeTab === 'knowledge' && agent && (
        <KnowledgePage onBack={() => setTab('chat')} />
      )}
    </>
  )
}
