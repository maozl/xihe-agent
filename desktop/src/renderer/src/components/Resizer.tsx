import { useEffect, useRef, useState } from 'react'
import { cn } from '../lib/cn'

/** Draggable panel edge shared by every resizable panel. Pointer-capture drag
 *  reports the raw pointer position; the caller derives its size from its own
 *  anchor element/edges. The internal flag keeps hover-moves inert. */
export function Resizer({ axis, onMove, className, title }: {
  /** 'row' = horizontal bar dragged vertically (height); 'col' = vertical bar
   *  dragged horizontally (width). */
  axis: 'row' | 'col'
  onMove: (clientX: number, clientY: number) => void
  className?: string
  title?: string
}) {
  const draggingRef = useRef(false)
  return (
    <div
      role="separator"
      aria-orientation={axis === 'row' ? 'horizontal' : 'vertical'}
      onPointerDown={(e) => {
        draggingRef.current = true
        e.currentTarget.setPointerCapture(e.pointerId)
      }}
      onPointerMove={(e) => {
        if (draggingRef.current) onMove(e.clientX, e.clientY)
      }}
      onPointerUp={() => {
        draggingRef.current = false
      }}
      onPointerCancel={() => {
        draggingRef.current = false
      }}
      title={title}
      className={cn(
        'z-20 hover:bg-sky-500/40 active:bg-sky-500/60',
        axis === 'row' ? 'cursor-row-resize' : 'cursor-col-resize',
        className
      )}
    />
  )
}

/** localStorage-backed panel size (px), clamped to min on load and set —
 *  callers clamp dynamic maxes (window bounds) before calling set. */
export function usePanelSize(key: string, def: number, min: number) {
  const [size, setSize] = useState(() => {
    const saved = Number(localStorage.getItem(key))
    return Number.isFinite(saved) && saved >= min ? saved : def
  })
  useEffect(() => {
    localStorage.setItem(key, String(size))
  }, [key, size])
  const set = (v: number): void => setSize(Math.max(v, min))
  return [size, set] as const
}
