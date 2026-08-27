import { Terminal, Boxes, MonitorUp, Shield, GitFork, Cpu } from 'lucide-react'
import type { EngineKind, AgentStatus } from '../store'
import { cn } from '../lib/cn'

const ENGINE_LABEL: Record<EngineKind, string> = {
  xihe: 'xihe',
  codebuddy: 'CodeBuddy'
}

const ENGINE_COLOR: Record<EngineKind, string> = {
  xihe: 'border-brand/30 bg-brand/15 text-brand',
  codebuddy: 'border-success/40 bg-success/10 text-success'
}

export function EngineBadge({ engine }: { engine: EngineKind }) {
  return (
    <span className={cn('rounded border px-1.5 py-0.5 text-[10px] font-medium', ENGINE_COLOR[engine])}>
      {ENGINE_LABEL[engine]}
    </span>
  )
}

export function StatusDot({ status }: { status: AgentStatus }) {
  const color = status === 'online' ? 'bg-success' : 'bg-strong'
  return <span className={cn('inline-block h-2 w-2 rounded-full', color)} title={status} />
}

const CAP_ICON: Record<string, typeof Cpu> = {
  shell: Terminal,
  browser: MonitorUp,
  mcp: Boxes,
  images: MonitorUp,
  interrupt: Shield,
  escalation: Shield,
  fork: GitFork
}

export function CapChip({ cap }: { cap: string }) {
  const Icon = CAP_ICON[cap] ?? Cpu
  return (
    <span className="inline-flex items-center gap-1 rounded bg-elevated/80 px-1.5 py-0.5 text-[11px] text-ink-2">
      <Icon className="h-3 w-3" />
      {cap}
    </span>
  )
}
