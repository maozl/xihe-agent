import { useEffect, useRef } from 'react'
import type * as MonacoNs from 'monaco-editor'
import { ensureMonaco } from './env'
import { cn } from '../../lib/cn'

interface Props {
  oldText: string
  newText: string
  oldHeader?: string
  newHeader?: string
  language?: string
  className?: string
}

/** Read-only side-by-side diff (monaco DiffEditor), lazily loaded. Models are
 *  transient — unlike MonacoPane there is no identity worth preserving for a
 *  one-shot comparison, so they're disposed with the editor. */
export function MonacoDiffPane({
  oldText,
  newText,
  oldHeader,
  newHeader,
  language,
  className,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<MonacoNs.editor.IStandaloneDiffEditor | null>(null)

  useEffect(() => {
    let disposed = false
    let ro: ResizeObserver | null = null
    void ensureMonaco().then((monaco) => {
      if (disposed || !hostRef.current) return
      const original = monaco.editor.createModel(oldText, language)
      const modified = monaco.editor.createModel(newText, language)
      const editor = monaco.editor.createDiffEditor(hostRef.current, {
        readOnly: true,
        renderSideBySide: true,
        originalEditable: false,
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
        fontSize: 12,
        lineHeight: 18,
        scrollBeyondLastLine: false,
        automaticLayout: false,
        renderOverviewRuler: false,
      })
      editor.setModel({
        original,
        modified,
      })
      ro = new ResizeObserver(() => editor.layout())
      ro.observe(hostRef.current)
      editorRef.current = editor
    })
    return () => {
      disposed = true
      ro?.disconnect()
      const editor = editorRef.current
      if (editor) {
        const model = editor.getModel()
        editor.dispose()
        model?.original.dispose()
        model?.modified.dispose()
      }
      editorRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A later props change (e.g. the approval diff re-resolving the old side
  // from disk) updates the models in place instead of rebuilding the editor.
  useEffect(() => {
    const model = editorRef.current?.getModel()
    if (!model) return
    if (model.original.getValue() !== oldText) model.original.setValue(oldText)
    if (model.modified.getValue() !== newText) model.modified.setValue(newText)
  }, [oldText, newText])

  return (
    <div className={cn('flex h-full w-full flex-col', className)}>
      {(oldHeader || newHeader) && (
        <div className="flex shrink-0 border-b border-line text-[10px] text-ink-4">
          <span className="flex-1 truncate px-2 py-1">{oldHeader ?? '磁盘'}</span>
          <span className="flex-1 truncate border-l border-line px-2 py-1">{newHeader ?? '变更'}</span>
        </div>
      )}
      <div ref={hostRef} className="min-h-0 flex-1" />
    </div>
  )
}
