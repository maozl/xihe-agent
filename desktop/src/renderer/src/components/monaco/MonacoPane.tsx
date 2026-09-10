import { useEffect, useRef } from 'react'
import type * as MonacoNs from 'monaco-editor'
import { ensureMonaco, getOrCreateModel } from './env'
import { cn } from '../../lib/cn'
import { diffLineMarks, type LineMark } from '../../lib/lineDiff'

interface Props {
  /** File identity — the Monaco model is keyed by its Uri, so the same path
   *  reuses its model (undo history survives tab switches). */
  path: string
  /** Canonical text (from disk). A change that doesn't match the model is
   *  pushed in — that's the external-reload path, not typing. */
  value: string
  language?: string
  readOnly?: boolean
  minimap?: boolean
  wordWrap?: boolean
  onChange?: (value: string) => void
  /** Ctrl/Cmd+S — bound as a monaco command so it doesn't fire when focus is
   *  outside the editor. */
  onSave?: () => void
  /** Git-HEAD base text for gutter change marks (IDEA-style: added/modified/
   *  deleted bars next to the line numbers). null/undefined = no marks. */
  headText?: string | null
  className?: string
}

// monaco.editor.OverviewRulerLane.Full — a value import here would pull the
// whole editor into every chunk that imports this module's types.
const OVERVIEW_FULL = 7

const MARK_MARGIN: Record<LineMark['kind'], string> = {
  added: 'xihe-git-added',
  modified: 'xihe-git-modified',
  deleted: 'xihe-git-deleted',
}
// Canvas-drawn (overview ruler) colors must be literal — CSS vars don't
// resolve there. Close enough in both themes.
const MARK_RULER: Record<LineMark['kind'], string> = {
  added: '#3fb950',
  modified: '#58a6ff',
  deleted: '#8b949e',
}

function markDecorations(marks: LineMark[]): MonacoNs.editor.IModelDeltaDecoration[] {
  return marks.map((m) => ({
    range: { startLineNumber: m.line, startColumn: 1, endLineNumber: m.line, endColumn: 1 },
    options: {
      marginClassName: MARK_MARGIN[m.kind],
      overviewRuler: { color: MARK_RULER[m.kind], position: OVERVIEW_FULL },
    },
  }))
}

/** Single-file Monaco editor, loaded lazily (the env module keeps monaco out
 *  of the main bundle). Callbacks are held in refs so re-renders never
 *  re-create the editor — only `path` does. */
export function MonacoPane({
  path,
  value,
  language,
  readOnly,
  minimap,
  wordWrap,
  onChange,
  onSave,
  headText,
  className,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<MonacoNs.editor.IStandaloneCodeEditor | null>(null)
  const onChangeRef = useRef(onChange)
  const onSaveRef = useRef(onSave)
  onChangeRef.current = onChange
  onSaveRef.current = onSave
  const headTextRef = useRef(headText)
  headTextRef.current = headText
  const marksRef = useRef<MonacoNs.editor.IEditorDecorationsCollection | null>(null)

  const applyMarks = (): void => {
    const editor = editorRef.current
    const model = editor?.getModel()
    if (!editor || !model) return
    const head = headTextRef.current
    if (head == null) {
      marksRef.current?.set([])
      return
    }
    // Clamp to the model's real line count — a trailing '\n' makes split()
    // yield one phantom line beyond what the editor shows.
    const max = model.getLineCount()
    marksRef.current?.set(
      markDecorations(diffLineMarks(head, model.getValue()).filter((m) => m.line <= max))
    )
  }

  useEffect(() => {
    let disposed = false
    let ro: ResizeObserver | null = null
    void ensureMonaco().then((monaco) => {
      if (disposed || !hostRef.current) return
      const model = getOrCreateModel(monaco, path, value, language)
      if (model.getValue() !== value) model.setValue(value)
      const editor = monaco.editor.create(hostRef.current, {
        model,
        readOnly: !!readOnly,
        minimap: { enabled: !!minimap },
        wordWrap: wordWrap ? 'on' : 'off',
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
        fontSize: 12,
        lineHeight: 18,
        scrollBeyondLastLine: false,
        renderWhitespace: 'selection',
        tabSize: 4,
        automaticLayout: false,
      })
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSaveRef.current?.())
      editor.onDidChangeModelContent(() => onChangeRef.current?.(editor.getValue()))
      // Manual layout: automaticLayout polls on an interval; RO is exact.
      ro = new ResizeObserver(() => editor.layout())
      ro.observe(hostRef.current)
      editorRef.current = editor
      marksRef.current = editor.createDecorationsCollection()
      applyMarks()
    })
    return () => {
      disposed = true
      ro?.disconnect()
      editorRef.current?.dispose()
      editorRef.current = null
      marksRef.current = null
    }
    // Language is creation-time only; path is the editor identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path])

  // External reload: disk text moved under an unchanged model (agent write,
  // save from elsewhere). Typing already went out through onChange, so a
  // mismatch here is always an external push.
  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    const model = editor.getModel()
    if (model && model.getValue() !== value) model.setValue(value)
  }, [value])

  useEffect(() => {
    editorRef.current?.updateOptions({ readOnly: !!readOnly })
  }, [readOnly])

  // Gutter change marks vs the HEAD base — debounced so a burst of keystrokes
  // settles before the diff runs. Only content/base changes re-run it.
  useEffect(() => {
    const t = setTimeout(applyMarks, 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, headText, path])

  return <div ref={hostRef} className={cn('h-full w-full', className)} />
}
