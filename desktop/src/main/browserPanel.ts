// BrowserPanelController — window-snap bridge for the desktop browser panel.
//
// The renderer's browser panel is a placeholder; the real Chrome (xihe's CDP
// browser) is moved over it by serve's /browser/* API (see
// src/gateway/browser_window.py). This controller lives in main because
// window events, the native HWND, and the DPI math only exist here; the
// renderer reports the placeholder's CSS-px rect over `browser:setSnapRegion`
// and never does scaling itself.

import { BrowserWindow, screen } from 'electron'

/** Placeholder rect in CSS px, relative to the window's content area. */
export interface SnapRect {
  x: number
  y: number
  w: number
  h: number
}

// Two top-level windows chasing each other mid-drag flicker; re-snap trails
// the drag instead (debounce), never follows it live.
const MOVE_DEBOUNCE_MS = 250
// After serve restarts, its snap state is gone — re-snap once it settles.
const RESNAP_DELAY_MS = 1_200
const RESNAP_MAX_TRIES = 3
const FETCH_MS = 3_000

export class BrowserPanelController {
  private readonly getBaseUrl: () => string
  private readonly getWin: () => BrowserWindow | null
  private cssRect: SnapRect | null = null
  // False while a full-window page (settings/store) covers the chat layout.
  // Window-event snaps/hides check it, so a focus event on tab switch can't
  // race the panel-hide by re-showing Chrome after it.
  private panelActive = true
  private snapFlying = false
  // Identity of the cssRect last sent to serve — a drag replaces cssRect with
  // a fresh object every frame; reference comparison is the pending check.
  private lastSentCss: SnapRect | null = null
  private moveTimer: ReturnType<typeof setTimeout> | null = null
  private resnapTries = 0
  private resnapTimer: ReturnType<typeof setTimeout> | null = null
  private attachedWin: BrowserWindow | null = null
  private readonly handlers: Array<[string, () => void]> = []

  constructor(opts: { getBaseUrl: () => string; getWin: () => BrowserWindow | null }) {
    this.getBaseUrl = opts.getBaseUrl
    this.getWin = opts.getWin
  }

  /** Renderer drove the placeholder in/out of the layout. rect=null un-tracks;
   *  mode splits the two un-track semantics — 'float' (PinOff) releases the
   *  Chrome visible beside the app, 'hide' (panel close, tab switch) keeps it
   *  out of the way until the panel comes back. */
  setSnapRegion(rect: SnapRect | null, mode: 'float' | 'hide' = 'hide'): void {
    if (
      rect &&
      (!Number.isFinite(rect.x) ||
        !Number.isFinite(rect.y) ||
        !Number.isFinite(rect.w) ||
        !Number.isFinite(rect.h))
    ) {
      return
    }
    this.cssRect = rect
    if (rect) {
      this.attachWindowEvents()
      void this.postSnap()
    } else {
      this.detachWindowEvents()
      this.lastSentCss = null
      if (mode === 'float') void this.postJson('/browser/release', {})
      else void this.postJson('/browser/hide', {})
    }
  }

  /** Renderer drove the chat layout's visibility (tab switch to a full-window
   *  page and back). Inactive → hide and stop event-driven snaps (the focus
   *  event of the switching click must not re-show Chrome); active → re-snap,
   *  which re-shows at the current rect. */
  setPanelActive(active: boolean): void {
    this.panelActive = active
    if (active) void this.postSnap(true)
    else void this.postJson('/browser/hide', {})
  }

  /** Serve transitioned to running — its snap state lives in serve memory, so
   *  a serve restart needs a fresh snap. Retried because the CDP Chrome may
   *  itself still be (re)starting and answer "no-chrome-window" at first. */
  onServeRunning(): void {
    if (!this.cssRect) return
    this.resnapTries = 0
    this.scheduleResnap()
  }

  /** before-quit, best-effort: the fetch may not complete before exit, which
   *  is fine — an un-released snap just leaves Chrome where it was. */
  release(): void {
    if (!this.cssRect) return
    this.cssRect = null
    this.lastSentCss = null
    this.detachWindowEvents()
    void this.postJson('/browser/release', {})
  }

  // ---- placement ----------------------------------------------------------

  /** CSS-px placeholder rect → physical-px screen rect covering it exactly.
   *  zoom maps CSS px into DIP (divide), dipToScreenPoint maps DIP to physical;
   *  zoom/scale/origin are re-read per call so cross-monitor moves and zoom
   *  changes track. x/y floor and w/h ceil — never a sub-pixel gap. */
  private physicalRect(win: BrowserWindow): { x: number; y: number; w: number; h: number } | null {
    const r = this.cssRect
    if (!r) return null
    const content = win.getContentBounds() // DIP
    const zoom = win.webContents.getZoomFactor() || 1
    const scale = screen.getDisplayMatching(content).scaleFactor || 1
    const origin = screen.dipToScreenPoint({ x: content.x, y: content.y })
    return {
      x: Math.floor(origin.x + (r.x / zoom) * scale),
      y: Math.floor(origin.y + (r.y / zoom) * scale),
      w: Math.ceil((r.w / zoom) * scale),
      h: Math.ceil((r.h / zoom) * scale),
    }
  }

  /** `force` sends even when cssRect is unchanged — window-event snaps (focus,
   *  move, restore) must fire on position/z-order changes that don't touch the
   *  cssRect reference. Layout-driven snaps (setSnapRegion) leave it false so
   *  the drag coalescing can skip redundant sends. */
  private async postSnap(force = false): Promise<boolean> {
    // Coalesced single-flight: at most one snap request is ever in flight.
    // While one runs, later callers no-op and this loop re-reads the NEWEST
    // rect after each response — a width-drag fires ~60 rects/s which would
    // otherwise queue as 60 unordered POSTs (the multi-second lag), while the
    // loop guarantees the final rect always lands.
    if (this.snapFlying) {
      if (force) this.lastSentCss = null // re-send after the in-flight one lands
      return false
    }
    this.snapFlying = true
    try {
      let ok = false
      while (
        this.cssRect &&
        this.panelActive &&
        (force || this.cssRect !== this.lastSentCss)
      ) {
        force = false
        this.lastSentCss = this.cssRect
        ok = await this.sendSnap()
      }
      return ok
    } finally {
      this.snapFlying = false
    }
  }

  private async sendSnap(): Promise<boolean> {
    const win = this.getWin()
    if (!win || win.isDestroyed() || !this.cssRect) return false
    const rect = this.physicalRect(win)
    if (!rect) return false
    // Buffer → HWND; readUInt32LE matches Windows pointer width for HWNDs
    // in practice (handles are 32-bit significant even on x64).
    const desktopHwnd = win.getNativeWindowHandle().readUInt32LE(0)
    const j = await this.postJson('/browser/snap', { ...rect, desktop_hwnd: desktopHwnd })
    return j?.ok === true
  }

  private scheduleResnap(): void {
    if (this.resnapTimer) clearTimeout(this.resnapTimer)
    this.resnapTimer = setTimeout(() => {
      this.resnapTimer = null
      void (async () => {
        const ok = await this.postSnap()
        if (!ok && this.resnapTries < RESNAP_MAX_TRIES) {
          this.resnapTries += 1
          this.scheduleResnap()
        }
      })()
    }, RESNAP_DELAY_MS)
  }

  // ---- window events --------------------------------------------------------

  private attachWindowEvents(): void {
    const win = this.getWin()
    if (!win || win.isDestroyed() || win === this.attachedWin) return
    this.detachWindowEvents()

    const onMove = (): void => {
      if (this.moveTimer) clearTimeout(this.moveTimer)
      this.moveTimer = setTimeout(() => {
        this.moveTimer = null
        void this.postSnap(true)
      }, MOVE_DEBOUNCE_MS)
    }
    const onSnap = (): void => void this.postSnap(true)
    const onHide = (): void => void this.postJson('/browser/hide', {})
    // focus→snap is the z-order repair: clicking desktop UI after clicking
    // Chrome raises the desktop window over Chrome; this puts Chrome back.
    // blur→hide relies on serve's foreground guard to no-op when the new
    // focus IS the Chrome window (clicking into Chrome blurs us but isn't
    // "left the app").
    const events: Array<[string, () => void]> = [
      ['move', onMove],
      ['resize', onMove],
      ['maximize', onSnap],
      ['unmaximize', onSnap],
      ['minimize', onHide],
      ['enter-full-screen', onHide],
      ['restore', onSnap],
      ['leave-full-screen', onSnap],
      ['focus', onSnap],
      ['blur', onHide],
    ]
    // Electron's on/off are per-event literal overloads; a string variable
    // can't match them, so route both attach and detach through one cast.
    const on = (ev: string, fn: () => void): void => {
      win.on(ev as never, fn)
    }
    for (const [ev, fn] of events) {
      on(ev, fn)
      this.handlers.push([ev, fn])
    }
    on('closed', this.onClosed)
    this.handlers.push(['closed', this.onClosed])
    this.attachedWin = win
  }

  private readonly onClosed = (): void => {
    this.detachWindowEvents()
  }

  private detachWindowEvents(): void {
    if (this.moveTimer) {
      clearTimeout(this.moveTimer)
      this.moveTimer = null
    }
    if (this.resnapTimer) {
      clearTimeout(this.resnapTimer)
      this.resnapTimer = null
    }
    const win = this.attachedWin
    for (const [ev, fn] of this.handlers) {
      if (win && !win.isDestroyed()) win.off(ev as never, fn)
    }
    this.handlers.length = 0
    this.attachedWin = null
  }

  // ---- serve HTTP -----------------------------------------------------------

  private async postJson(
    path: string,
    body: unknown
  ): Promise<Record<string, unknown> | null> {
    try {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), FETCH_MS)
      try {
        const r = await fetch(this.getBaseUrl() + path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        })
        return (await r.json()) as Record<string, unknown>
      } finally {
        clearTimeout(t)
      }
    } catch (e) {
      // Best-effort by design (serve down mid-drag etc.), but never silent.
      console.log(`[browserPanel] ${path} failed:`, e instanceof Error ? e.message : e)
      return null
    }
  }
}
