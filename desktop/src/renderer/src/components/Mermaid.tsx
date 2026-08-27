import { useEffect, useState } from 'react'
import mermaid from 'mermaid'
import { useStore } from '../store'

let seq = 0

function isLight(theme: string) {
  return (
    theme === 'light' ||
    (theme === 'system' && document.documentElement.dataset.theme === 'light')
  )
}

export default function Mermaid({ code }: { code: string }) {
  const theme = useStore((s) => s.theme)
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: isLight(theme) ? 'default' : 'dark',
        })
        const { svg } = await mermaid.render(`mmd-${seq++}`, code)
        if (!cancelled) {
          setSvg(svg)
          setError('')
        }
      } catch (e) {
        // LLM-generated mermaid is often slightly off — surface the source
        // instead of a blank hole.
        if (!cancelled) setError(String((e as Error)?.message ?? e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code, theme])

  if (error) {
    return (
      <div className="mb-2.5">
        <div className="mb-1 text-[10px] text-warning/80">mermaid 渲染失败，显示源码</div>
        <pre className="overflow-x-auto rounded-lg bg-elevated p-3 text-[13px] leading-relaxed text-ink-2">
          {code}
        </pre>
      </div>
    )
  }
  return (
    <div
      className="my-2.5 overflow-x-auto [&_svg]:mx-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}
