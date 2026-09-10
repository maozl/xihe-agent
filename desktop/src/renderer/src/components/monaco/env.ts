// Monaco bootstrap shared by MonacoPane / MonacoDiffPane. Everything here is
// only reachable through React.lazy boundaries, so monaco (+ its workers) all
// land in lazy chunks and the chat-first paint stays light. Nothing in this
// module may be statically imported from App/store — the worker imports below
// would drag the whole editor into the main bundle.
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import JsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import TsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'
import type * as MonacoNs from 'monaco-editor'
import { useStore } from '../../store'

type Monaco = typeof MonacoNs

let monacoP: Promise<Monaco> | null = null

function resolvedDark(theme: string): boolean {
  if (theme === 'system') return window.matchMedia('(prefers-color-scheme: dark)').matches
  return theme === 'dark'
}

// The app theme lives on <html data-theme>; Monaco can't read CSS variables,
// so these are hardcoded mirrors of the index.css palettes, swapped on the
// same store signal App.tsx uses.
function defineThemes(monaco: Monaco): void {
  monaco.editor.defineTheme('xihe-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '737373', fontStyle: 'italic' },
      { token: 'keyword', foreground: '7dd3fc' },
      { token: 'string', foreground: '34d399' },
      { token: 'number', foreground: 'fcd34d' },
      { token: 'type', foreground: 'c4b5fd' },
      { token: 'type.identifier', foreground: 'c4b5fd' },
      { token: 'function', foreground: '93c5fd' },
    ],
    colors: {
      'editor.background': '#171717',
      'editor.foreground': '#ebebeb',
      'editorLineNumber.foreground': '#525252',
      'editorLineNumber.activeForeground': '#a3a3a3',
      'editor.selectionBackground': '#404040',
      'editor.lineHighlightBackground': '#1f1f1f',
      'editorCursor.foreground': '#7dd3fc',
      'editorIndentGuide.background': '#2a2a2a',
      'diffEditor.insertedTextBackground': '#34d39926',
      'diffEditor.removedTextBackground': '#fb718526',
    },
  })
  monaco.editor.defineTheme('xihe-light', {
    base: 'vs',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '8e929a', fontStyle: 'italic' },
      { token: 'keyword', foreground: '0369a1' },
      { token: 'string', foreground: '047857' },
      { token: 'number', foreground: 'b45309' },
      { token: 'type', foreground: '6d28d9' },
      { token: 'type.identifier', foreground: '6d28d9' },
      { token: 'function', foreground: '1d4ed8' },
    ],
    colors: {
      'editor.background': '#fcfcfd',
      'editor.foreground': '#181b20',
      'editorLineNumber.foreground': '#8a8e96',
      'editorLineNumber.activeForeground': '#686d76',
      'editor.selectionBackground': '#e6e8ec',
      'editor.lineHighlightBackground': '#f0f1f4',
      'editorCursor.foreground': '#0369a1',
      'editorIndentGuide.background': '#dfe1e6',
      'diffEditor.insertedTextBackground': '#0478571f',
      'diffEditor.removedTextBackground': '#be123c1f',
    },
  })
}

/** Idempotent load-and-configure of monaco (memoized promise). Applies the
 *  current store theme at load and follows later switches, including the
 *  system-mode OS flip App.tsx also reacts to. */
export function ensureMonaco(): Promise<Monaco> {
  if (!monacoP) {
    monacoP = import('monaco-editor').then((monaco) => {
      // Editor worker always; ts/json language services where they exist.
      // Other languages tokenize via Monarch in-page — they only lose
      // semantic validation, which we don't wire workers for anyway.
      ;(self as unknown as { MonacoEnvironment?: unknown }).MonacoEnvironment = {
        getWorker: (_workerId: string, label: string): Worker => {
          if (label === 'json') return new JsonWorker()
          if (label === 'javascript' || label === 'typescript') return new TsWorker()
          return new EditorWorker()
        },
      }
      defineThemes(monaco)
      monaco.editor.setTheme(resolvedDark(useStore.getState().theme) ? 'xihe-dark' : 'xihe-light')
      useStore.subscribe((s, prev) => {
        if (s.theme !== prev.theme)
          monaco.editor.setTheme(resolvedDark(s.theme) ? 'xihe-dark' : 'xihe-light')
      })
      const mq = window.matchMedia('(prefers-color-scheme: dark)')
      mq.addEventListener('change', () => {
        if (useStore.getState().theme === 'system')
          monaco.editor.setTheme(mq.matches ? 'xihe-dark' : 'xihe-light')
      })
      return monaco
    })
  }
  return monacoP
}

/** Models are keyed by file Uri and never disposed here — switching tabs and
 *  back keeps undo history; the model set is bounded by files opened this
 *  session. A disk-side delete + reopen is covered by the external-reload
 *  path (value effect in MonacoPane). */
export function getOrCreateModel(
  monaco: Monaco,
  path: string,
  value: string,
  language?: string
): MonacoNs.editor.ITextModel {
  const uri = monaco.Uri.file(path)
  return (
    monaco.editor.getModel(uri) ??
    monaco.editor.createModel(value, language ?? undefined, uri)
  )
}
