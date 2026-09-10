import { useEffect, useRef, useState } from 'react'
import { Pin, PinOff, Play, RefreshCw } from 'lucide-react'
import { desktop } from '../lib/desktop'
import {
  getBrowserStatus,
  launchBrowser,
  showBrowser,
  type BrowserStatus,
} from '../lib/serveClient'
import { cn } from '../lib/cn'

interface Props {
  /** Whether the chat layout (this panel's home) is the visible tab. False on
   *  the full-window settings/store pages → the snapped Chrome hides (like a
   *  minimize) instead of being released to float. */
  active: boolean
  className?: string
}

const POLL_MS = 5_000
// While waiting for a cold start (agent just triggered the browser), poll fast
// so the snap lands ASAP once the CDP port comes up.
const WAITING_POLL_MS = 1_500

// Chrome's own minimum window width is ~500 physical px — below this the serve
// snap clamps and the window no longer covers the placeholder.
const MIN_WIDTH = 520
const DEFAULT_WIDTH = 560
const WIDTH_KEY = 'browserPanelWidth'

/** Agent browser panel — a placeholder the real CDP Chrome is snapped over
 *  (Win32, in main + serve). This component only reports the placeholder's
 *  CSS-px rect over IPC and shows lifecycle state; there is no iframe, no
 *  screenshot stream — the Chrome over it IS the panel. */
export function BrowserPanel({ active, className }: Props) {
  const [status, setStatus] = useState<BrowserStatus | null>(null)
  const [docked, setDocked] = useState(true)
  const [launching, setLaunching] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [width, setWidth] = useState(() => {
    const saved = Number(localStorage.getItem(WIDTH_KEY))
    return Number.isFinite(saved) && saved >= MIN_WIDTH ? saved : DEFAULT_WIDTH
  })
  const placeholderRef = useRef<HTMLDivElement>(null)
  const draggingRef = useRef(false)
  // Mirror for the tracking effect (stable deps): adding `active` as an effect
  // dep would re-run the effect on every tab switch, whose cleanup releases
  // the snap — the opposite of what a tab switch should do.
  const activeRef = useRef(active)
  activeRef.current = active
  // Same staleness concern for the polling loop (its effect deps are [refreshKey]
  // only): the auto-restore reads docked live.
  const dockedRef = useRef(docked)
  dockedRef.current = docked

  useEffect(() => {
    localStorage.setItem(WIDTH_KEY, String(width))
  }, [width])

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    const tick = async () => {
      const s = await getBrowserStatus()
      if (!alive) return
      setStatus(s)
      setLaunching(false)
      // Hidden while this panel is the visible tab: focus already returned to
      // the desktop window, so the chrome should be back too — the event-path
      // resnap either raced or was missed; restore it directly. Inactive tabs
      // must NOT fire this (a hidden chrome is the expected state there).
      if (s?.hidden && s.snapped && activeRef.current && dockedRef.current) {
        void showBrowser()
      }
      timer = setTimeout(() => void tick(), s?.running ? POLL_MS : WAITING_POLL_MS)
    }
    void tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [refreshKey])

  // Track the placeholder: report its CSS-px rect on layout changes so main
  // can snap Chrome over it. Un-tracking (docked off, Chrome gone, panel
  // unmount) releases the Chrome window to float. Element rect is relative to
  // the viewport = the window content area, which is exactly the coordinate
  // space main's DPI math expects — no scaling happens here.
  const tracking = docked && !!status?.running

  const reportRect = (): void => {
    const el = placeholderRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    if (r.width < 1 || r.height < 1) return
    void desktop.setBrowserSnapRegion({ x: r.x, y: r.y, w: r.width, h: r.height })
  }

  useEffect(() => {
    if (!tracking) return
    const el = placeholderRef.current
    if (!el) return
    if (activeRef.current) reportRect()
    const ro = new ResizeObserver(() => {
      // Suppressed during a width-handle drag — one snap on release instead
      // (per-frame cross-process SetWindowPos forces a Chrome re-layout each
      // time; batching to one call at the end is why the drag felt laggy).
      if (!draggingRef.current && activeRef.current) reportRect()
    })
    ro.observe(el)
    window.addEventListener('resize', reportRect)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', reportRect)
      // Cleanup fires for three reasons; dockedRef separates them: PinOff
      // (docked just flipped off) floats the Chrome, panel unmount (Monitor
      // off) hides it — the button is show/hide, not float. Chrome-death
      // also lands on 'hide', a harmless no-window POST.
      void desktop.setBrowserSnapRegion(null, dockedRef.current ? 'hide' : 'float')
    }
  }, [tracking])

  // Tab-switch visibility goes through MAIN (not a direct serve call): main
  // hides/re-snaps and gates its window-event snaps on the flag, so the focus
  // event of the switching click can't re-show the just-hidden Chrome.
  // `tracking` is read live, not a dep — its own effect owns it.
  useEffect(() => {
    if (!tracking) return
    void desktop.setBrowserPanelActive(active)
  }, [active])

  // Width handle (panel's left edge). Pointer capture keeps the drag alive
  // outside the handle; the chat pane keeps at least 420px.
  const onHandleDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    draggingRef.current = true
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onHandleMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (!draggingRef.current) return
    const max = Math.max(window.innerWidth - 420, MIN_WIDTH)
    setWidth(Math.min(Math.max(window.innerWidth - e.clientX, MIN_WIDTH), max))
  }
  const onHandleUp = (): void => {
    draggingRef.current = false
    // Next frame: the last setWidth of the drag is committed to the DOM by
    // then, so the rect read here is the final one (reading synchronously in
    // the pointerup handler can still see the pre-commit layout).
    requestAnimationFrame(() => {
      if (activeRef.current) reportRect()
    })
  }

  const onLaunch = async (): Promise<void> => {
    setLaunching(true)
    const s = await launchBrowser()
    if (s) setStatus(s)
    setLaunching(false)
  }

  const running = !!status?.running
  // Chrome dragged away while snapped (window rect drifted from the snap
  // rect) — the next focus on this window re-snaps it; surface the state.
  const dragged =
    docked &&
    running &&
    !!status?.snapped &&
    !!status?.rect &&
    (Math.abs((status.rect[0] ?? 0) - (status.snapped[0] ?? 0)) > 24 ||
      Math.abs((status.rect[1] ?? 0) - (status.snapped[1] ?? 0)) > 24)

  return (
    <aside className={cn('relative flex flex-col bg-app', className)} style={{ width }}>
      <div
        role="separator"
        aria-orientation="vertical"
        onPointerDown={onHandleDown}
        onPointerMove={onHandleMove}
        onPointerUp={onHandleUp}
        onPointerCancel={onHandleUp}
        title="拖动调整面板宽度"
        className="absolute left-0 top-0 z-20 h-full w-1.5 -translate-x-1/2 cursor-col-resize hover:bg-sky-500/40 active:bg-sky-500/60"
      />
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <span
          className={cn(
            'h-2 w-2 shrink-0 rounded-full',
            running ? 'bg-success' : 'bg-strong'
          )}
          title={running ? 'Chrome 运行中' : 'Chrome 未运行'}
        />
        <span className="text-sm font-semibold">浏览器</span>
        <span className="text-xs text-ink-4">:{status?.port ?? '—'}</span>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setDocked((v) => !v)}
            disabled={!running}
            title={docked ? '取消吸附，Chrome 独立浮动' : '吸附 Chrome 到面板'}
            className={cn(
              'rounded p-1.5 transition',
              docked && running
                ? 'text-accent hover:bg-elevated'
                : 'text-ink-3 hover:bg-elevated',
              !running && 'cursor-not-allowed opacity-40'
            )}
          >
            {docked ? <PinOff className="h-4 w-4" /> : <Pin className="h-4 w-4" />}
          </button>
          <button
            onClick={() => setRefreshKey((k) => k + 1)}
            title="刷新状态"
            className="rounded p-1.5 text-ink-3 transition hover:bg-elevated hover:text-ink"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 p-3">
        {!running ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-line text-ink-4">
            <p className="text-xs">agent 的 Chrome 未运行</p>
            <button
              onClick={() => void onLaunch()}
              disabled={launching}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition',
                launching
                  ? 'cursor-wait bg-elevated text-ink-3'
                  : 'bg-sky-600 text-white hover:bg-sky-500'
              )}
            >
              <Play className="h-3.5 w-3.5" />
              {launching ? '启动中…（最多 8s）' : '启动浏览器'}
            </button>
            <p className="max-w-[16rem] text-center text-[10px] leading-relaxed text-ink-4">
              登录态与 CLI 的 agent 浏览器共用（cdp-profile），在 CLI 登过的不用重登
            </p>
          </div>
        ) : !docked ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-line text-ink-4">
            <Pin className="h-5 w-5 text-ink-4" />
            <p className="text-xs">浏览器已浮动</p>
            <button
              onClick={() => setDocked(true)}
              className="rounded-md bg-elevated px-3 py-1.5 text-xs text-ink transition hover:bg-strong"
            >
              重新吸附到面板
            </button>
          </div>
        ) : (
          <div
            ref={placeholderRef}
            className="relative h-full w-full overflow-hidden rounded-lg border border-line bg-panel"
          >
            {/* The snapped Chrome covers this element exactly; keep a hint
                under it for the (rare) frames where Chrome lags a move. */}
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-[11px] text-ink-4">
              浏览器吸附区（Chrome 覆盖此处）
            </div>
            {dragged && (
              <div className="absolute inset-x-0 top-0 z-10 border-t border-warning/40 bg-warning/10 px-2 py-1 text-center text-[10px] text-warning">
                Chrome 被移出吸附区 — 点一下本窗口任意处自动贴回
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}
