import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Bot,
  CalendarClock,
  Check,
  ChevronLeft,
  Cpu,
  Pencil,
  Palette,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Square,
  Trash2,
  ToggleRight,
  Zap,
} from 'lucide-react'
import { useStore } from '../store'
import type { Agent } from '../store'
import { cn } from '../lib/cn'
import { desktop, type XiheConfigPatch, type ThemeMode } from '../lib/desktop'
import {
  cronAction,
  deleteCron,
  getBrowserStatus,
  listCronRuns,
  listSpecialists,
  putSpecialist,
  deleteSpecialist,
  restartBrowser,
  testConnection,
  type CronJobInfo,
  type CronRunInfo,
  type SpecialistSpec,
  type SpecialistEntry,
  type SpecialistMcpServer,
  type SpecialistToolset,
  type SpecialistsInfo,
  type SkillInfo,
} from '../lib/serveClient'

/** Left-nav category list. Icons appear only here, never on the cards (Halo
 *  convention). Two groups: read-only resources, then editable config. */
const NAV: { group: string; items: { id: string; label: string; icon: typeof Cpu }[] }[] = [
  {
    group: '资源',
    items: [
      { id: 'specialists', label: '专家 Agents', icon: Bot },
      { id: 'cron', label: '调度', icon: CalendarClock },
    ],
  },
  {
    group: '配置',
    items: [
      { id: 'model', label: '模型与连接', icon: Cpu },
      { id: 'behavior', label: '行为与安全', icon: ShieldCheck },
      { id: 'capability', label: '能力开关', icon: ToggleRight },
      { id: 'appearance', label: '外观', icon: Palette },
    ],
  },
]
const SECTION_IDS = NAV.flatMap((g) => g.items.map((i) => i.id))

interface CfgForm {
  model: string
  baseUrl: string
  apiKey: string
  visionModel: string
  maxIter: string
  compThresh: string
  approvalsMode: string
  redact: boolean
  kbs: boolean
  specialists: boolean
  imageGen: boolean
  tts: boolean
}

/** Settings page — renders full-window (own Header + back arrow, hides the
 *  chat sidebar while active). Halo-style: a w-48 left nav (scroll-spy jumps,
 *  doesn't switch panes) + a single-column scroll of section cards, plus an
 *  always-visible bottom action bar for the config commit. xihe reads all of
 *  its config from one ~/.xihe-agent/config.yaml and needs a write + restart to
 *  apply, so there's no per-field auto-save like Halo — the commit is explicit.
 *  Instance resources (MCP/skills/cron) are process-level, pulled read-only
 *  from serve via loadManageData. */
export function SettingsPanel({ agent, onBack }: { agent: Agent; onBack: () => void }) {
  const serveConnected = useStore((s) => s.serveConnected)
  const xiheStatus = useStore((s) => s.xiheStatus)
  const cronJobs = useStore((s) => s.cronJobs)
  const schedulerHealth = useStore((s) => s.schedulerHealth)
  const loadManageData = useStore((s) => s.loadManageData)
  const xiheConfig = useStore((s) => s.xiheConfig)
  const saveXiheConfig = useStore((s) => s.saveXiheConfig)

  useEffect(() => {
    void loadManageData()
  }, [serveConnected, loadManageData])

  // Config form state lives here (lifted out of a child) so the bottom action
  // bar can reach save/restart. apiKey is write-only: blank in the form, and
  // blank-on-save means "keep the existing key".
  const [cfg, setCfg] = useState<CfgForm>({
    model: '', baseUrl: '', apiKey: '', visionModel: '', maxIter: '', compThresh: '',
    approvalsMode: 'manual', redact: true, kbs: false, specialists: false,
    imageGen: false, tts: false,
  })
  const [status, setStatus] = useState<null | { kind: 'ok' | 'err'; msg: string }>(null)
  const [restarting, setRestarting] = useState(false)
  // Restart-watch for the save flow: xiheStatus is ALREADY 'running' when save()
  // fires, so success can't be "saw running" — the restart must first push a
  // non-running state ('starting'), and only the 'running' AFTER that resolves
  // the status line. Phases live in a ref so the effect re-runs on every
  // xihe:status push without re-arming.
  const restartWatch = useRef<'off' | 'left-running' | 'awaiting-running'>('off')
  // Bumped on every save so a stale 45s fallback timer can't fire into a newer
  // restart-watch cycle.
  const restartSeq = useRef(0)

  useEffect(() => {
    const w = restartWatch.current
    if (w === 'off') return
    const st = xiheStatus?.state
    if (st === 'errored' || st === 'not_found') {
      restartWatch.current = 'off'
      setStatus({ kind: 'err', msg: `重启失败：${xiheStatus?.message ?? st}` })
      return
    }
    if (w === 'left-running' && st !== 'running') {
      restartWatch.current = 'awaiting-running'
    } else if (w === 'awaiting-running' && st === 'running') {
      restartWatch.current = 'off'
      setStatus({ kind: 'ok', msg: 'xihe已重启，配置已生效' })
    }
  }, [xiheStatus])
  // POST /test-connection outcome for the 模型与连接 card. Runs in serve with
  // the SAVED config.yaml values (the renderer holds no api_key), so unsaved
  // form edits don't participate — the hint next to the button says so.
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<null | { ok: boolean; text: string }>(null)

  const testConn = async () => {
    setTesting(true)
    setTestResult(null)
    const r = await testConnection()
    setTesting(false)
    if (!r) {
      setTestResult({ ok: false, text: '探测失败：serve 未响应' })
      return
    }
    if (r.ok) {
      const found = r.models.length ? `，发现 ${r.models.length} 个模型` : ''
      const note = r.error ? `（${r.error}）` : ''
      setTestResult({ ok: true, text: `连接正常${found}${note}` })
    } else {
      setTestResult({ ok: false, text: r.error ?? `HTTP ${r.status_code}` })
    }
  }

  useEffect(() => {
    setCfg({
      model: xiheConfig.model ?? '',
      baseUrl: xiheConfig.base_url ?? '',
      apiKey: '',
      visionModel: xiheConfig.vision_model ?? '',
      maxIter: String(xiheConfig.max_iterations ?? ''),
      compThresh: String(xiheConfig.compression_threshold ?? ''),
      approvalsMode: xiheConfig.approvals_mode ?? 'manual',
      redact: xiheConfig.redact_enabled ?? true,
      kbs: xiheConfig.kbs_enabled ?? false,
      specialists: xiheConfig.specialists_enabled ?? false,
      imageGen: xiheConfig.image_gen_enabled ?? false,
      tts: xiheConfig.tts_enabled ?? false,
    })
  }, [xiheConfig])

  const update = <K extends keyof CfgForm>(k: K, v: CfgForm[K]) =>
    setCfg((s) => ({ ...s, [k]: v }))

  // Diff local edits against effective values; only changed keys go into the
  // patch (so an untouched field's line is never rewritten in config.yaml).
  // dirtyCount drives the bottom bar's copy + button emphasis.
  const dirtyCount = useMemo(() => {
    let n = 0
    if (cfg.apiKey) n++ // write-only: any non-blank input is a change
    if (cfg.model.trim() !== (xiheConfig.model ?? '')) n++
    if (cfg.baseUrl.trim() !== (xiheConfig.base_url ?? '')) n++
    if (cfg.visionModel.trim() !== (xiheConfig.vision_model ?? '')) n++
    if (cfg.approvalsMode !== (xiheConfig.approvals_mode ?? 'manual')) n++
    if (cfg.redact !== (xiheConfig.redact_enabled ?? true)) n++
    if (cfg.kbs !== (xiheConfig.kbs_enabled ?? false)) n++
    if (cfg.specialists !== (xiheConfig.specialists_enabled ?? false)) n++
    if (cfg.imageGen !== (xiheConfig.image_gen_enabled ?? false)) n++
    if (cfg.tts !== (xiheConfig.tts_enabled ?? false)) n++
    if (cfg.maxIter.trim() !== '') {
      const mi = Number(cfg.maxIter)
      if (Number.isFinite(mi) && mi !== (xiheConfig.max_iterations ?? 30)) n++
    }
    if (cfg.compThresh.trim() !== '') {
      const ct = Number(cfg.compThresh)
      if (Number.isFinite(ct) && ct !== (xiheConfig.compression_threshold ?? 0.5)) n++
    }
    return n
  }, [cfg, xiheConfig])

  const buildPatch = (): XiheConfigPatch => {
    const p: XiheConfigPatch = {}
    const m = cfg.model.trim()
    if (m !== (xiheConfig.model ?? '')) p.model = m
    const bu = cfg.baseUrl.trim()
    if (bu !== (xiheConfig.base_url ?? '')) p.base_url = bu
    const vm = cfg.visionModel.trim()
    if (vm !== (xiheConfig.vision_model ?? '')) p.vision_model = vm
    if (cfg.maxIter.trim() !== '') {
      const mi = Number(cfg.maxIter)
      if (Number.isFinite(mi) && mi !== (xiheConfig.max_iterations ?? 30)) p.max_iterations = mi
    }
    if (cfg.compThresh.trim() !== '') {
      const ct = Number(cfg.compThresh)
      if (Number.isFinite(ct) && ct !== (xiheConfig.compression_threshold ?? 0.5)) {
        p.compression_threshold = ct
      }
    }
    if (cfg.approvalsMode !== (xiheConfig.approvals_mode ?? 'manual')) p.approvals_mode = cfg.approvalsMode
    if (cfg.redact !== (xiheConfig.redact_enabled ?? true)) p.redact_enabled = cfg.redact
    if (cfg.kbs !== (xiheConfig.kbs_enabled ?? false)) p.kbs_enabled = cfg.kbs
    if (cfg.specialists !== (xiheConfig.specialists_enabled ?? false)) {
      p.specialists_enabled = cfg.specialists
    }
    if (cfg.imageGen !== (xiheConfig.image_gen_enabled ?? false)) p.image_gen_enabled = cfg.imageGen
    if (cfg.tts !== (xiheConfig.tts_enabled ?? false)) p.tts_enabled = cfg.tts
    if (cfg.apiKey) p.api_key = cfg.apiKey
    return p
  }

  const save = async () => {
    const patch = buildPatch()
    if (Object.keys(patch).length === 0) {
      setStatus({ kind: 'ok', msg: '没有改动' })
      return
    }
    const ok = await saveXiheConfig(patch)
    if (!ok) {
      setStatus({ kind: 'err', msg: '保存失败：配置文件无法写入，请检查文件权限' })
      return
    }
    setCfg((s) => ({ ...s, apiKey: '' }))
    // Config only takes effect after a serve restart (xihe reads config.yaml
    // at boot), and saving IS the user's apply intent — restart immediately
    // instead of requiring a second click (first-launch reports: key saved,
    // serve never restarted).
    setStatus({ kind: 'ok', msg: '已保存，正在重启…' })
    setRestarting(true)
    restartWatch.current = 'left-running'
    const seq = ++restartSeq.current
    await desktop.serveRestart()
    setTimeout(() => setRestarting(false), 1500)
    // Safety net: if no status push ever lands (wedged restart), unstick the
    // status line instead of leaving "正在重启…" forever.
    setTimeout(() => {
      if (restartSeq.current === seq && restartWatch.current !== 'off') {
        restartWatch.current = 'off'
        setStatus({ kind: 'err', msg: '重启超时 — 请看左下角连接状态，或再点一次「重启xihe」' })
      }
    }, 45_000)
  }

  const restart = async () => {
    setRestarting(true)
    setStatus(null)
    await desktop.serveRestart()
    // xihe:status push drives starting→running; clear the spinner shortly.
    setTimeout(() => setRestarting(false), 1500)
  }

  const activeSection = useScrollSpy(SECTION_IDS)
  const jumpTo = (id: string) =>
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return (
    <div className="flex h-screen flex-col bg-app text-ink">
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <button
          onClick={onBack}
          title="返回对话"
          className="rounded-md p-1 text-ink-3 transition hover:bg-elevated hover:text-ink"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <h1 className="text-sm font-semibold">设置</h1>
      </header>
      {!serveConnected && (
        <div
          className={cn(
            'border-b px-6 py-2 text-[11px]',
            xiheStatus?.state === 'not_found'
              ? 'border-danger/40 bg-danger/10 text-danger/90'
              : 'border-warning/40 bg-warning/10 text-warning/80'
          )}
        >
          {xiheStatus?.state === 'not_found'
            ? '未找到 xihe 程序 — 请先安装（pip install -e .），或用 XIHE_BIN 指定路径后重试'
            : xiheStatus?.state === 'errored' && xiheStatus.message
              ? xiheStatus.message
              : xiheStatus?.state === 'stopped'
                ? 'xihe未运行，正在尝试重启…'
                : 'xihe未就绪，正在启动…'}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <SettingsNav active={activeSection} onJump={jumpTo} />

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-6 px-6 py-6">

            <SpecialistsCard />

            <CronCard />

            <Card id="model" title="模型与连接">
              <div className="divide-y divide-line">
                <FieldRow label="模型" required>
                  <input value={cfg.model} onChange={(e) => update('model', e.target.value)} className={textCls} />
                </FieldRow>
                <FieldRow label="Base URL">
                  <input
                    value={cfg.baseUrl}
                    onChange={(e) => update('baseUrl', e.target.value)}
                    placeholder="https://…"
                    className={textCls}
                  />
                </FieldRow>
                <FieldRow
                  label={xiheConfig.api_key_set ? 'API Key（已配置 · 留空保持不变）' : 'API Key（未配置）'}
                >
                  <input
                    type="password"
                    value={cfg.apiKey}
                    onChange={(e) => update('apiKey', e.target.value)}
                    placeholder={xiheConfig.api_key_set ? '留空保持不变' : '粘贴 key 后保存'}
                    className={textCls}
                  />
                </FieldRow>
                <FieldRow label="视觉模型" hint="识别图片用的模型；留空则不支持图片理解">
                  <input
                    value={cfg.visionModel}
                    onChange={(e) => update('visionModel', e.target.value)}
                    placeholder="如 gpt-4o"
                    className={textCls}
                  />
                </FieldRow>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
                <button
                  onClick={() => void testConn()}
                  disabled={testing || !serveConnected}
                  title="用已保存的连接信息测试；未保存的修改不参与"
                  className="flex items-center gap-1 rounded-lg border border-line-strong px-3 py-1 text-xs text-ink-2 transition hover:bg-elevated disabled:opacity-40"
                >
                  <RefreshCw className={cn('h-3 w-3', testing && 'animate-spin')} />
                  {testing ? '测试中…' : '测试连接'}
                </button>
                {testResult && (
                  <span
                    className={cn(
                      'text-[11px]',
                      testResult.ok ? 'text-success/80' : 'text-danger/80'
                    )}
                  >
                    {testResult.text}
                  </span>
                )}
                {testResult === null && dirtyCount > 0 && (
                  <span className="text-[11px] text-warning/70">
                    有未保存改动 — 测试连接用的是已保存值，建议先保存
                  </span>
                )}
              </div>
            </Card>

            <Card id="behavior" title="行为与安全">
              <div className="divide-y divide-line">
                <FieldRow label="最大迭代数" hint="单次任务最多执行多少轮工具调用">
                  <input
                    type="number"
                    value={cfg.maxIter}
                    onChange={(e) => update('maxIter', e.target.value)}
                    className={numCls}
                  />
                </FieldRow>
                <FieldRow label="压缩阈值" hint="对话太长时自动总结压缩历史（0–1，数值越大越晚压缩）">
                  <input
                    type="number"
                    step={0.05}
                    min={0}
                    max={1}
                    value={cfg.compThresh}
                    onChange={(e) => update('compThresh', e.target.value)}
                    className={numCls}
                  />
                </FieldRow>
                <FieldRow label="审批模式" hint="改文件、跑命令等敏感操作会先征求你的确认">
                  <select
                    value={cfg.approvalsMode}
                    onChange={(e) => update('approvalsMode', e.target.value)}
                    className={selCls}
                  >
                    <option value="manual">manual（需确认）</option>
                    <option value="auto">auto（自动执行）</option>
                  </select>
                </FieldRow>
                <Toggle
                  label="日志脱敏"
                  hint="日志里的密钥、令牌自动打码"
                  checked={cfg.redact}
                  onChange={(v) => update('redact', v)}
                />
              </div>
            </Card>

            <Card id="capability" title="能力开关">
              <div className="divide-y divide-line">
                <Toggle
                  label="业务知识库（.biz_kbs）"
                  hint="允许 agent 检索业务知识库"
                  checked={cfg.kbs}
                  onChange={(v) => update('kbs', v)}
                />
                <Toggle
                  label="专家 agent 委派"
                  hint="允许你自建的专家 agent 参与任务分工（产品内置专家不受影响）"
                  checked={cfg.specialists}
                  onChange={(v) => update('specialists', v)}
                />
                <Toggle
                  label="图像生成"
                  hint="允许 agent 生成图片"
                  checked={cfg.imageGen}
                  onChange={(v) => update('imageGen', v)}
                />
                <Toggle
                  label="语音合成（TTS）"
                  hint="允许 agent 把文字转成语音"
                  checked={cfg.tts}
                  onChange={(v) => update('tts', v)}
                />
              </div>
            </Card>

            <AppearanceCard />

            <p className="px-1 text-[11px] text-ink-4">
              更多高级选项（消息平台、MCP 服务、搜索等）需编辑配置文件 ~/.xihe-agent/config.yaml。
            </p>
          </div>
        </main>
      </div>

      <div className="flex items-center justify-between gap-3 border-t border-line bg-app px-6 py-3">
        <div className="min-w-0 flex-1 text-[11px]">
          {status ? (
            <span
              className={cn(status.kind === 'ok' ? 'text-success/80' : 'text-danger/80')}
            >
              {status.msg}
            </span>
          ) : dirtyCount > 0 ? (
            <span className="text-warning/80">
              有 {dirtyCount} 项改动待保存 — 保存后将自动重启并生效
            </span>
          ) : (
            <span className="text-ink-4">
              保存后自动重启生效；专家 Agents 与外观的修改在各自页面单独保存
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => void save()}
            disabled={!cfg.model.trim() || dirtyCount === 0}
            className={cn(
              'rounded-lg px-3 py-1.5 text-xs text-white transition disabled:opacity-40',
              dirtyCount > 0
                ? 'bg-brand hover:bg-brand/90'
                : 'border border-line-strong bg-transparent !text-ink-4'
            )}
          >
            保存{dirtyCount > 0 ? `（${dirtyCount} 项）` : ''}
          </button>
          <button
            onClick={() => void restart()}
            disabled={restarting}
            className="flex items-center gap-1 rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-2 transition hover:bg-elevated disabled:opacity-40"
          >
            <RefreshCw className={cn('h-3 w-3', restarting && 'animate-spin')} />
            重启xihe
          </button>
        </div>
      </div>
    </div>
  )
}

/** Appearance card — desktop theme (segmented, click-to-apply). Theming is a
 *  desktop capability: the mode lives in ~/.xihe-desktop/settings.json (never
 *  xihe config.yaml); main flips nativeTheme + persists + pushes the resolved
 *  light/dark to xihe. A running Chrome applies the pushed appearance only on
 *  its next launch, so the card offers a one-click restart while one is up. */
function AppearanceCard() {
  const theme = useStore((s) => s.theme)
  const setTheme = useStore((s) => s.setTheme)
  const [chromeRunning, setChromeRunning] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [restartNote, setRestartNote] = useState('')

  // One-shot probe — only decides whether to show the restart affordance.
  useEffect(() => {
    void getBrowserStatus().then((s) => setChromeRunning(!!s?.running))
  }, [])

  const pick = (t: ThemeMode) => {
    void setTheme(t)
    setRestartNote('')
  }

  const restartChrome = async () => {
    setRestarting(true)
    setRestartNote('')
    const s = await restartBrowser()
    setRestarting(false)
    setChromeRunning(!!s?.running)
    setRestartNote(
      s?.restarted ? '已重启，Chrome 已应用当前配色' : '重启失败，稍后可在浏览器面板重试'
    )
  }

  const OPTIONS: { id: ThemeMode; label: string }[] = [
    { id: 'dark', label: '深色' },
    { id: 'light', label: '浅色' },
    { id: 'system', label: '跟随系统' },
  ]

  return (
    <Card id="appearance" title="外观">
      <div className="divide-y divide-line">
        <FieldRow
          label="主题"
          hint="桌面本地设置，浏览器面板的 Chrome 跟随此选择；浅色下若系统是深色，Chrome 界面仍会跟随系统变暗（Chrome 无强制浅色开关）"
        >
          <div className="flex overflow-hidden rounded-lg border border-line">
            {OPTIONS.map((o) => (
              <button
                key={o.id}
                onClick={() => pick(o.id)}
                className={cn(
                  'px-3 py-1.5 text-xs transition',
                  theme === o.id
                    ? 'bg-contrast text-white'
                    : 'text-ink-3 hover:bg-elevated hover:text-ink'
                )}
              >
                {o.label}
              </button>
            ))}
          </div>
        </FieldRow>
      </div>
      {chromeRunning && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-ink-3">
          <span>浏览器正在运行，新配色将在其重启后生效</span>
          <button
            onClick={() => void restartChrome()}
            disabled={restarting}
            className="rounded border border-warning/50 px-2 py-0.5 text-warning transition hover:bg-warning/20 disabled:opacity-40"
          >
            {restarting ? '重启中…' : '重启浏览器'}
          </button>
          {restartNote && <span>{restartNote}</span>}
        </div>
      )}
    </Card>
  )
}

/** 调度卡片 — 定时任务的只读详情视图。任务通过对话创建（agent 的
 *  cronjob 工具，origin 自动携带正确的投递目标），这里不提供编辑/新建/
 *  行操作——管理动作回到对话里说一句即可（改时间/暂停/删除）。 */
function CronCard() {
  const serveConnected = useStore((s) => s.serveConnected)
  const cronJobs = useStore((s) => s.cronJobs)
  const schedulerHealth = useStore((s) => s.schedulerHealth)
  const loadManageData = useStore((s) => s.loadManageData)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 点击"立即运行"后短暂显示 ✓，给用户确认反馈（无状态条）
  const [ranOk, setRanOk] = useState<string | null>(null)
  const ranTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // run-now 后延迟刷新：短任务几秒后才落 last_run_at/输出文件，立刻刷看不到
  const refreshTimers = useRef<ReturnType<typeof setTimeout>[]>([])
  // 展开行时拉取的最近运行记录（组件级：展开区渲染在 map 内部）
  const [runs, setRuns] = useState<CronRunInfo[]>([])
  useEffect(() => {
    if (!expanded) return
    let stale = false
    setRuns([])
    void listCronRuns(expanded, 5).then((r) => {
      if (!stale) setRuns(r)
    })
    return () => {
      stale = true
    }
  }, [expanded])
  useEffect(
    () => () => {
      if (ranTimer.current) clearTimeout(ranTimer.current)
      refreshTimers.current.forEach(clearTimeout)
    },
    []
  )

  const fmt = (iso: string | null | undefined) => {
    if (!iso) return '—'
    return iso.slice(2, 16).replace('T', ' ')
  }

  const act = async (fn: () => Promise<{ ok: boolean; error?: string }>) => {
    setBusy(true)
    const r = await fn()
    setBusy(false)
    if (!r?.ok) return false
    await loadManageData()
    return true
  }

  const run = (j: CronJobInfo) =>
    void act(() => cronAction(j.job_id, 'run')).then((ok) => {
      if (!ok) return
      setRanOk(j.job_id)
      if (ranTimer.current) clearTimeout(ranTimer.current)
      ranTimer.current = setTimeout(() => setRanOk(null), 2000)
      // 立即运行是异步落盘的——5s/15s 后补两次刷新，覆盖短任务完成
      refreshTimers.current.forEach(clearTimeout)
      refreshTimers.current = [5000, 15000].map((ms) =>
        setTimeout(() => void loadManageData(), ms)
      )
    })
  const toggle = (j: CronJobInfo) =>
    void act(() => cronAction(j.job_id, j.enabled ? 'pause' : 'resume'))
  const remove = async (j: CronJobInfo) => {
    if (!window.confirm(`删除定时任务「${j.name || j.job_id}」？`)) return
    if (await act(() => deleteCron(j.job_id))) {
      if (expanded === j.job_id) setExpanded(null)
      await loadManageData()
    }
  }

  return (
    <Card
      id="cron"
      title={`调度（${cronJobs.length}）`}
      badge={
        schedulerHealth ? (
          <span className="flex items-center gap-1 text-[11px] text-ink-4">
            <Dot on={schedulerHealth.alive} />
            {schedulerHealth.alive ? '调度器运行中' : '调度器未运行'}
          </span>
        ) : undefined
      }
      headerRight={
        <button
          onClick={() => void loadManageData()}
          title="刷新"
          className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      }
    >
      {!serveConnected ? (
        <Empty>xihe未连接。</Empty>
      ) : cronJobs.length === 0 ? (
        <Empty>还没有定时任务。在对话里让 agent 建一个即可（如"每天早上 9 点提醒我站会"）。</Empty>
      ) : (
        <div className="space-y-1">
          {/* 防御性去重：serve 数据层无重复（已验证），但React的key碰撞
              在极端时序下可能渲染出幽灵行——按job_id去重保证UI永不重复 */}
          {cronJobs
            .filter((j, i, arr) => arr.findIndex((x) => x.job_id === j.job_id) === i)
            .map((j) => (
            <div key={j.job_id}>
              <div
                className={cn(
                  'flex w-full items-center gap-2 overflow-hidden whitespace-nowrap rounded-lg border border-line px-3 py-1.5 text-left transition',
                  expanded === j.job_id ? 'bg-elevated' : 'bg-panel/60 hover:bg-elevated/50',
                  !j.enabled && 'opacity-50'
                )}
              >
                <Dot on={j.enabled && j.state !== 'completed'} />
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => setExpanded(expanded === j.job_id ? null : j.job_id)}
                  onKeyDown={(e) => e.key === 'Enter' && setExpanded(expanded === j.job_id ? null : j.job_id)}
                  className="flex min-w-0 flex-1 cursor-pointer select-none items-center gap-2 overflow-hidden"
                >
                  <span className="shrink-0 truncate text-xs font-medium text-ink">{j.name || j.job_id}</span>
                  <span className="shrink-0 rounded bg-elevated px-1 py-0.5 font-mono text-[10px] text-ink-3">
                    {j.schedule}
                  </span>
                  {j.no_agent ? <Tag>纯脚本</Tag> : <Tag>AI</Tag>}
                  <span className="min-w-0 flex-1" />
                  {j.last_status && (
                    <span
                      className={cn(
                        'shrink-0 text-[10px]',
                        j.last_status === 'ok' ? 'text-success/80' : 'text-danger/80'
                      )}
                      title={j.last_error ?? ''}
                    >
                      上次 {fmt(j.last_run_at)} {j.last_status === 'ok' ? '✓' : '✗'}
                    </span>
                  )}
                  <span className="shrink-0 text-[10px] text-ink-4">
                    {!j.enabled
                      ? j.state === 'completed'
                        ? '已完成'
                        : '已暂停'
                      : `下次 ${fmt(j.next_run_at)}`}
                  </span>
                  <ChevronLeft
                    className={cn(
                      'h-3 w-3 shrink-0 text-ink-4 transition',
                      expanded === j.job_id ? '-rotate-90' : 'rotate-90'
                    )}
                  />
                </div>
                <span className="flex shrink-0 gap-0.5">
                  <button
                    onClick={() => void run(j)}
                    disabled={busy}
                    title="立即运行一次"
                    className={cn(
                      'rounded p-1 transition hover:bg-elevated disabled:opacity-30',
                      ranOk === j.job_id ? 'text-success' : 'text-ink-4 hover:text-ink-2'
                    )}
                  >
                    {ranOk === j.job_id ? <Check className="h-3 w-3" /> : <Zap className="h-3 w-3" />}
                  </button>
                  <button
                    onClick={() => void toggle(j)}
                    disabled={busy}
                    title={j.enabled ? '暂停（不再触发）' : '恢复'}
                    className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-warning disabled:opacity-30"
                  >
                    {j.enabled ? <Square className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                  </button>
                  <button
                    onClick={() => void remove(j)}
                    disabled={busy}
                    title="删除"
                    className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-danger disabled:opacity-30"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </span>
              </div>
              {expanded === j.job_id && (
                <div className="mt-1 space-y-1 rounded-lg border border-line bg-panel/40 px-3 py-2 text-[11px]">
                  <DetailRow label="任务 ID">{j.job_id}</DetailRow>
                  <DetailRow label="重复">{j.repeat}</DetailRow>
                  <DetailRow label="投递">
                    {j.deliver ?? 'origin'}
                    {j.origin ? `（origin = ${j.origin.platform}:${j.origin.chat_id}）` : ''}
                  </DetailRow>
                  {!j.no_agent && j.prompt && <DetailRow label="提示词">{j.prompt}</DetailRow>}
                  {j.script && <DetailRow label="脚本">$ {j.script}</DetailRow>}
                  <DetailRow label="下次运行">{j.next_run_at ?? '—'}</DetailRow>
                  <DetailRow label="上次运行">
                    {j.last_run_at ?? '—'}
                    {j.last_status ? ` · ${j.last_status}` : ''}
                  </DetailRow>
                  {j.last_error && (
                    <DetailRow label="上次错误">
                      <span className="text-danger/80">{j.last_error}</span>
                    </DetailRow>
                  )}
                  <div className="pt-1">
                    <div className="mb-1 text-ink-4">最近运行</div>
                    {runs.length === 0 ? (
                      <div className="text-ink-4">暂无运行记录</div>
                    ) : (
                      runs.map((r) => (
                        <div
                          key={r.run_at}
                          title={r.content}
                          className="truncate text-ink-3"
                        >
                          {fmt(r.run_at)}{' '}
                          {(r.content.split('\n').find((l) => l.trim()) ?? '').slice(0, 100)}
                        </div>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-16 shrink-0 text-ink-4">{label}</span>
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-all text-ink-2">{children}</span>
    </div>
  )
}

/** Specialist-agents editor card — one file per agent under
 *  `<agent_home>/agents/<slug>.yaml`. Unlike the read-only resource cards,
 *  this is a CRUD form: list → inline edit / create / delete → per-file
 *  PUT/DELETE via serve. Dispatch tools only change after a serve restart
 *  (the global 重启xihe button in the bottom bar). */
function SpecialistsCard() {
  const serveConnected = useStore((s) => s.serveConnected)
  const skillCatalog = useStore((s) => s.skills)
  const [info, setInfo] = useState<SpecialistsInfo | null>(null)
  // slug being edited, '__new__' while creating, null = list view
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [keySet, setKeySet] = useState(false)
  const [status, setStatus] = useState<null | { kind: 'ok' | 'err'; msg: string }>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    const r = await listSpecialists()
    if (r) setInfo(r)
  }, [])

  useEffect(() => {
    if (serveConnected) void load()
  }, [serveConnected, load])

  const beginEdit = (e: SpecialistEntry) => {
    setEditing(e.slug)
    setKeySet(e.api_key_set)
    setDraft({
      slug: e.slug,
      name: e.spec.name ?? '',
      description: e.spec.description ?? '',
      persona: e.spec.persona ?? '',
      // base 强制保底：置顶且始终选中（旧配置没写也会补上）
      toolsets: ['base', ...(e.spec.toolsets ?? []).filter((t) => t !== 'base')],
      skills: [...(e.spec.skills ?? [])],
      model: e.spec.model ?? '',
      baseUrl: e.spec.base_url ?? '',
      apiKey: '',
      maxIterations: e.spec.max_iterations != null ? String(e.spec.max_iterations) : '',
      projectContext: e.spec.project_context ?? false,
      enabled: e.spec.enabled ?? true,
    })
    setStatus(null)
  }

  const beginCreate = () => {
    setEditing('__new__')
    setKeySet(false)
    setDraft({
      slug: '',
      name: '',
      description: '',
      persona: '',
      toolsets: ['base'],
      skills: [],
      model: '',
      baseUrl: '',
      apiKey: '',
      maxIterations: '',
      projectContext: false,
      enabled: true,
    })
    setStatus(null)
  }

  const cancel = () => {
    setEditing(null)
    setDraft(null)
    setStatus(null)
  }

  const persist = async (slug: string, spec: SpecialistSpec, successMsg: string): Promise<boolean> => {
    setSaving(true)
    const r = await putSpecialist(slug, spec)
    setSaving(false)
    if (!r?.ok) {
      setStatus({ kind: 'err', msg: '保存失败：后台服务未连接或目录不可写' })
      return false
    }
    setStatus({
      kind: 'ok',
      msg: r.warnings?.length
        ? `${successMsg}；校验告警：${r.warnings.join('；')}`
        : `${successMsg}，已实时生效`,
    })
    await load()
    return true
  }

  const saveDraft = async () => {
    if (!draft || !info) return
    const slug = draft.slug.trim()
    if (!/^[a-z][a-z0-9_]{1,30}$/.test(slug)) {
      setStatus({ kind: 'err', msg: 'slug 需为小写字母/数字/下划线，且以字母开头（≥2 位）' })
      return
    }
    if (info.specialists.some((e) => e.slug === slug) && editing !== slug) {
      setStatus({ kind: 'err', msg: `slug "${slug}" 已存在` })
      return
    }
    // 内置专家：保存只写连接覆盖 delta（serve 拒内容键）；api_key 留空 =
    // 不随 body 发送 = 保持已有
    if (editing !== '__new__' && info.specialists.find((e) => e.slug === slug)?.bundled) {
      const mi = draft.maxIterations.trim() === '' ? undefined : Number(draft.maxIterations)
      const delta: SpecialistSpec = {
        model: draft.model.trim() || undefined,
        base_url: draft.baseUrl.trim() || undefined,
        api_key: draft.apiKey || undefined,
        max_iterations: mi !== undefined && Number.isFinite(mi) ? mi : undefined,
      }
      if (await persist(slug, delta, `已更新内置专家 ${slug} 的连接覆盖（delta）`)) cancel()
      return
    }
    if (!draft.description.trim() || !draft.persona.trim()) {
      setStatus({ kind: 'err', msg: 'description 与 persona 为必填（路由与身份依据）' })
      return
    }
    const mi = draft.maxIterations.trim() === '' ? undefined : Number(draft.maxIterations)
    // 编辑既有专家时以原始 spec 为底——表单没覆盖的键（如 sticky_session）
    // 原样保留，不会被一次编辑静默丢掉
    const base: SpecialistSpec = editing !== '__new__'
      ? info.specialists.find((e) => e.slug === editing)?.spec ?? {}
      : {}
    const spec: SpecialistSpec = {
      ...base,
      name: draft.name.trim() || undefined,
      description: draft.description.trim(),
      persona: draft.persona,
      toolsets: draft.toolsets.length ? draft.toolsets : ['base'],
      skills: draft.skills.length ? draft.skills : undefined,
      model: draft.model.trim() || undefined,
      base_url: draft.baseUrl.trim() || undefined,
      api_key: draft.apiKey || undefined,
      max_iterations: mi !== undefined && Number.isFinite(mi) ? mi : undefined,
      project_context: draft.projectContext,
      enabled: draft.enabled,
    }
    if (await persist(slug, spec, `已写入 agents/${slug}.yaml`)) cancel()
  }

  const remove = async (slug: string) => {
    if (!window.confirm(`删除专家 agent "${slug}"？将删除 agents/${slug}.yaml。`)) return
    setSaving(true)
    const ok = await deleteSpecialist(slug)
    setSaving(false)
    if (!ok) {
      setStatus({ kind: 'err', msg: '删除失败（serve 未连接）' })
      return
    }
    setStatus({ kind: 'ok', msg: `已删除 ${slug}，已实时生效` })
    if (editing === slug) cancel()
    await load()
  }

  /** 行内启停开关。内置专家：停用 = 写只含 enabled:false 的 delta（内置
   *  内容由产品托管，delta 升级不漂移）；重新启用 = 删除 delta，产品内置
   *  直接恢复（拿最新版）。用户专家：直接翻转 enabled 落盘。 */
  const toggleEnabled = async (e: SpecialistEntry) => {
    const nowOn = e.spec.enabled !== false
    if (e.bundled && nowOn) {
      setSaving(true)
      const ok = await deleteSpecialist(e.slug)
      setSaving(false)
      if (!ok) {
        setStatus({ kind: 'err', msg: '恢复失败（serve 未连接）' })
        return
      }
      setStatus({ kind: 'ok', msg: `已恢复内置 ${e.slug}，已实时生效` })
      await load()
      return
    }
    const msg = e.bundled
      ? `已停用 ${e.slug}（一行停用 delta；重新启用即删除恢复内置）`
      : nowOn ? `已停用 ${e.slug}` : `已启用 ${e.slug}`
    await persist(e.slug,
      e.bundled ? { enabled: false } : { ...e.spec, enabled: !nowOn },
      msg)
  }

  const entries = info ? info.specialists : []

  const update = <K extends keyof Draft>(k: K, v: Draft[K]) =>
    setDraft((d) => (d ? { ...d, [k]: v } : d))

  return (
    <Card
      id="specialists"
      title={`专家 Agents（${entries.length}）`}
      headerRight={
        <div className="flex items-center gap-1">
          <button
            onClick={() => void load()}
            title="刷新"
            className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={beginCreate}
            disabled={editing !== null}
            title="新建专家 agent"
            className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2 disabled:opacity-30"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      }
    >
      {!serveConnected ? (
        <Empty>xihe未连接。</Empty>
      ) : !info ? (
        <Empty>读取中…</Empty>
      ) : (
        <div className="space-y-2">
          {status && (
            <div
              className={cn(
                'rounded-lg border px-3 py-1.5 text-[11px]',
                status.kind === 'ok'
                  ? 'border-success/50 text-success/80'
                  : 'border-danger/50 text-danger/80'
              )}
            >
              {status.msg}
            </div>
          )}

          {!info.specialists_enabled && (
            <div className="rounded-lg border border-warning/50 bg-warning/10 px-3 py-1.5 text-[11px] text-warning/80">
              你自建的专家尚未启用——到「配置 → 能力开关 → 专家 agent 委派」打开并保存即可
              （产品内置专家不受此开关影响）。
            </div>
          )}

          {entries.length === 0 && editing === null && (
            <Empty>
              还没有专家 agent。点右上角 + 新建一个，或在 ~/.xihe-agent/agents/ 放一个 yaml 文件。
            </Empty>
          )}

          {editing === '__new__' && draft && (
            <DraftForm
              draft={draft}
              catalog={info.toolsets}
              mcpServers={info.mcp_servers ?? []}
              skillsCatalog={skillCatalog}
              creating
              apiKeySet={false}
              saving={saving}
              update={update}
              onCancel={cancel}
              onSave={() => void saveDraft()}
            />
          )}

          {entries.map((e) => (
            <div key={e.slug}>
              <div
                className={cn(
                  'flex items-center gap-2 rounded-lg border border-line bg-panel/60 px-3 py-1.5',
                  // 置灰停用的专家：整行降不透明度 + 去饱和，启用按钮仍可点
                  e.spec.enabled === false && 'opacity-50 grayscale',
                )}
              >
                <Dot on={e.spec.enabled !== false} />
                <span className="shrink-0 text-xs font-medium text-ink">
                  {e.spec.name || e.slug}
                </span>
                <span className="shrink-0 text-[10px] text-ink-4">{e.slug}</span>
                {e.bundled && <Tag tone="sky">内置</Tag>}
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-4">
                  {e.spec.description}
                </span>
                {(e.spec.toolsets ?? []).map((t) => (
                  <Tag key={t}>{t}</Tag>
                ))}
                {e.spec.enabled === false ? (
                  <Tag>已停用</Tag>
                ) : info.registered.includes(`run_${e.slug}_agent`) ? (
                  <Tag tone="emerald">已注册</Tag>
                ) : !e.bundled && !info.specialists_enabled ? (
                  <Tag>未启用</Tag>
                ) : (
                  <Tag tone="rose">未注册</Tag>
                )}
                <button
                  onClick={() => void toggleEnabled(e)}
                  disabled={saving}
                  title={e.spec.enabled !== false
                    ? '停用后不参与任务分工（配置保留，可随时再开）'
                    : e.bundled
                      ? '删除停用副本，恢复产品内置（最新版）'
                      : '重新启用'}
                  className={cn(
                    'shrink-0 rounded border px-1.5 py-0.5 text-[10px] transition disabled:opacity-30',
                    e.spec.enabled !== false
                      ? 'border-line-strong text-ink-4 hover:border-danger/50 hover:text-danger'
                      : 'border-success/50 text-success/80 hover:bg-success/10',
                  )}
                >
                  {e.spec.enabled !== false ? '停用' : '启用'}
                </button>
                <button
                  onClick={() => (editing === e.slug ? cancel() : beginEdit(e))}
                  title={editing === e.slug ? '收起' : e.bundled ? '查看（内置专家只读）' : '编辑'}
                  className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-ink-2"
                >
                  {editing === e.slug ? (
                    <ChevronLeft className="h-3 w-3 rotate-90" />
                  ) : (
                    <Pencil className="h-3 w-3" />
                  )}
                </button>
                {e.bundled ? (
                  <button
                    disabled
                    title="产品内置专家：编辑会生成用户覆盖副本，删除副本即恢复内置"
                    className="rounded p-1 text-ink-4 opacity-30"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                ) : (
                  <button
                    onClick={() => void remove(e.slug)}
                    title={e.slug === 'explore' || e.slug === 'xihe_guide'
                      ? '删除用户副本后将恢复同名内置专家'
                      : '删除'}
                    className="rounded p-1 text-ink-4 transition hover:bg-elevated hover:text-danger"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                )}
              </div>
              {editing === e.slug && draft && (
                <div className="mt-1.5">
                  <DraftForm
                    draft={draft}
                    catalog={info.toolsets}
                    mcpServers={info.mcp_servers ?? []}
                    skillsCatalog={skillCatalog}
                    apiKeySet={keySet}
                    saving={saving}
                    contentLocked={e.bundled}
                    update={update}
                    onCancel={cancel}
                    onSave={() => void saveDraft()}
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/** Editable form state for one specialist. Skills are a toggled name list;
 *  apiKey is write-only (blank = keep the stored key); maxIterations blank =
 *  inherit the main config. toolsets may hold static groups plus `mcp` /
 *  `mcp-<server>` names written by the MCP section. */
interface Draft {
  slug: string
  name: string
  description: string
  persona: string
  toolsets: string[]
  skills: string[]
  model: string
  baseUrl: string
  apiKey: string
  maxIterations: string
  projectContext: boolean
  enabled: boolean
}

/** MCP chip rows for the specialist form: 全部 (flat `mcp` toolset) plus one
 *  per configured server (`mcp-<name>`, dimmed when not connected). A name
 *  the spec lists but the catalog lacks (removed server) still renders dashed
 *  so it isn't silently dropped on save. */
function mcpChipData(
  servers: SpecialistMcpServer[],
  selected: string[]
): { key: string; label: string; on: boolean; dim: boolean; hint: string }[] {
  const chips = [
    {
      key: 'mcp',
      label: '全部',
      on: selected.includes('mcp'),
      dim: servers.length === 0,
      hint: '全部 MCP 服务器的工具（toolset: mcp）',
    },
  ]
  const names = new Set(servers.map((s) => s.name))
  for (const s of servers) {
    const key = `mcp-${s.name}`
    chips.push({
      key,
      label: s.name,
      on: selected.includes(key),
      dim: !s.connected,
      hint: `${s.name} · ${s.tools} 工具${s.connected ? '' : ' · 未连接'}`,
    })
  }
  for (const t of selected) {
    if (t.startsWith('mcp-') && !names.has(t.slice(4))) {
      chips.push({
        key: t,
        label: t.slice(4),
        on: true,
        dim: true,
        hint: '不在当前 MCP 配置中（仍保留在文件里）',
      })
    }
  }
  return chips
}

function DraftForm({
  draft,
  catalog,
  mcpServers,
  skillsCatalog,
  apiKeySet,
  creating,
  saving,
  contentLocked,
  update,
  onCancel,
  onSave,
}: {
  draft: Draft
  catalog: SpecialistToolset[]
  mcpServers: SpecialistMcpServer[]
  skillsCatalog: SkillInfo[]
  apiKeySet: boolean
  creating?: boolean
  saving: boolean
  /** 内置专家：内容区（slug/persona/工具集/技能…）锁定只读，连接参数区
   *  保持可编辑（delta 覆盖：model/base_url/api_key/max_iterations） */
  contentLocked?: boolean
  update: <K extends keyof Draft>(k: K, v: Draft[K]) => void
  onCancel: () => void
  onSave: () => void
}) {
  const toggleToolset = (t: string) => {
    if (t === 'base') return // 只读保底面：强制启用，不可取消
    update(
      'toolsets',
      draft.toolsets.includes(t)
        ? draft.toolsets.filter((x) => x !== t)
        : [...draft.toolsets, t]
    )
  }

  const toggleSkill = (s: string) =>
    update(
      'skills',
      draft.skills.includes(s)
        ? draft.skills.filter((x) => x !== s)
        : [...draft.skills, s]
    )

  // Chips show the live catalog plus any spec-listed name it no longer
  // contains (removed skill / other instance) — nothing is silently dropped
  // on save; unknown names render dashed and are kept.
  const knownSkills = new Set(skillsCatalog.map((s) => s.name))
  const skillChips: { name: string; hint: string; known: boolean }[] = [
    ...skillsCatalog.map((s) => ({
      name: s.name,
      hint: s.description || s.name,
      known: true,
    })),
    ...draft.skills
      .filter((n) => !knownSkills.has(n))
      .map((n) => ({ name: n, hint: '不在当前技能索引中（仍保留在文件里）', known: false })),
  ]

  return (
    <div className="space-y-2 rounded-lg border border-brand/30 bg-panel/60 p-3">
      {contentLocked && (
        <div className="rounded-lg border border-line bg-elevated px-3 py-1.5 text-[11px] text-ink-4">
          产品内置专家：上半部分内容只读（虚线示锁）；「模型与连接」可编辑——
          保存只写覆盖 delta，产品升级内容不受影响。停用请用行内开关；深度定制请新建专家。
        </div>
      )}
      <fieldset disabled={contentLocked} className="contents">
      <div className="flex items-center justify-between gap-4 py-1">
        <div className="min-w-0 flex-1">
          <p className="text-sm text-ink">
            slug <span className="text-danger">*</span>
          </p>
          <p className="text-xs text-ink-4">派生工具名 run_&lt;slug&gt;_agent，保存后不可改</p>
        </div>
        {creating ? (
          <input
            value={draft.slug}
            onChange={(e) => update('slug', e.target.value)}
            placeholder="如 data_analyst"
            className={cn(textCls, 'w-40')}
          />
        ) : (
          <code className="rounded bg-elevated px-2 py-1 text-xs text-ink-3">
            run_{draft.slug}_agent
          </code>
        )}
      </div>
      <div className="flex items-center justify-between gap-4 border-t border-line py-2">
        <p className="text-sm text-ink">
          名称 <span className="text-xs text-ink-4">（默认 = slug）</span>
        </p>
        <input
          value={draft.name}
          onChange={(e) => update('name', e.target.value)}
          className={cn(textCls, 'w-40')}
        />
      </div>
      <div className="border-t border-line py-2">
        <div className="mb-1 flex items-baseline gap-2">
          <p className="text-sm text-ink">
            职责描述 <span className="text-danger">*</span>
          </p>
          <p className="text-xs text-ink-4">主 agent 据此路由任务</p>
        </div>
        <textarea
          value={draft.description}
          onChange={(e) => update('description', e.target.value)}
          rows={2}
          placeholder="该专家负责什么"
          className="w-full resize-y rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30"
        />
      </div>
      <div className="border-t border-line py-2">
        <p className="mb-1 text-sm text-ink">
          persona <span className="text-danger">*</span>
          <span className="ml-2 text-xs text-ink-4">身份层全文替换（SOUL.md 不再生效）</span>
        </p>
        <textarea
          value={draft.persona}
          onChange={(e) => update('persona', e.target.value)}
          rows={4}
          placeholder="你是…，擅长…，做事原则…"
          className="w-full resize-y rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30"
        />
      </div>
      <div className="border-t border-line py-2">
        <p className="mb-1.5 text-sm text-ink">工具集</p>
        <div className="flex flex-wrap gap-1.5">
          {catalog
            .filter((t) => t.name !== 'mcp' && (t.tools > 0 || t.name === 'base'))
            .sort((a, b) => (a.name === 'base' ? -1 : b.name === 'base' ? 1 : 0))
            .map((t) => {
              const locked = t.name === 'base'
              const on = locked || draft.toolsets.includes(t.name)
              return (
                <button
                  key={t.name}
                  type="button"
                  title={locked
                    ? '基础只读面（读文件/技能/记忆/沙箱）——所有 agent 强制启用，不可取消'
                    : `${t.name} · ${t.description} · ${t.tools} 工具`}
                  onClick={() => toggleToolset(t.name)}
                  disabled={locked}
                  className={cn(
                    'rounded border px-2 py-0.5 text-[11px] transition',
                    on
                      ? 'border-brand bg-brand/10 text-brand'
                      : 'border-line-strong text-ink-4 hover:text-ink-2',
                    locked && 'cursor-default'
                  )}
                >
                  {t.label}
                </button>
              )
            })}
        </div>
      </div>
      <div className="border-t border-line py-2">
        <p className="mb-1.5 text-sm text-ink">
          MCP 服务器
          <span className="ml-2 text-xs text-ink-4">
            按需勾选 MCP 服务
          </span>
        </p>
        <div className="flex flex-wrap gap-1.5">
          {mcpChipData(mcpServers, draft.toolsets).map((c) => (
            <button
              key={c.key}
              type="button"
              title={c.hint}
              onClick={() => toggleToolset(c.key)}
              className={cn(
                'rounded border px-2 py-0.5 text-[11px] transition',
                c.on
                  ? 'border-brand bg-brand/10 text-brand'
                  : 'border-line-strong text-ink-4 hover:text-ink-2',
                c.dim && !c.on && 'border-dashed opacity-70'
              )}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
      <div className="border-t border-line py-2">
        <p className="mb-1.5 text-sm text-ink">
          技能白名单
          <span className="ml-2 text-xs text-ink-4">不选 = 不注入任何技能</span>
        </p>
        <div className="flex flex-wrap gap-1.5">
          {skillChips.length === 0 && (
            <p className="text-[11px] text-ink-4">技能索引为空</p>
          )}
          {skillChips.map((c) => {
            const on = draft.skills.includes(c.name)
            return (
              <button
                key={c.name}
                type="button"
                title={c.hint}
                onClick={() => toggleSkill(c.name)}
                className={cn(
                  'rounded border px-2 py-0.5 text-[11px] transition',
                  on
                    ? 'border-brand bg-brand/10 text-brand'
                    : 'border-line-strong text-ink-4 hover:text-ink-2',
                  !c.known && 'border-dashed'
                )}
              >
                {c.name}
              </button>
            )
          })}
        </div>
      </div>
      </fieldset>
      <div className="divide-y divide-line border-t border-line">
        <p className="py-2 text-xs font-medium uppercase tracking-wider text-ink-4">
          模型与连接（留空则沿用主模型设置）
          {contentLocked && (
            <span className="ml-1 normal-case tracking-normal text-brand/80">
              · 内置专家仅此区可覆盖
            </span>
          )}
        </p>
        <FieldRow label="模型">
          <input
            value={draft.model}
            onChange={(e) => update('model', e.target.value)}
            placeholder="如 glm-5.2-air"
            className={cn(textCls, 'w-40')}
          />
        </FieldRow>
        <FieldRow label="Base URL">
          <input
            value={draft.baseUrl}
            onChange={(e) => update('baseUrl', e.target.value)}
            placeholder="https://…"
            className={cn(textCls, 'w-56')}
          />
        </FieldRow>
        <FieldRow label={apiKeySet ? 'API Key（已配置 · 留空保持不变）' : 'API Key（未配置）'}>
          <input
            type="password"
            value={draft.apiKey}
            onChange={(e) => update('apiKey', e.target.value)}
            placeholder={apiKeySet ? '留空保持不变' : '粘贴 key 后保存'}
            className={cn(textCls, 'w-56')}
          />
        </FieldRow>
        <FieldRow label="最大迭代数">
          <input
            type="number"
            value={draft.maxIterations}
            onChange={(e) => update('maxIterations', e.target.value)}
            placeholder="继承"
            className={numCls}
          />
        </FieldRow>
      </div>
      <fieldset disabled={contentLocked} className="contents">
      <div className="divide-y divide-line border-t border-line">
        <Toggle
          label="读取项目上下文"
          hint="开启后该专家会读工作目录的 CLAUDE.md / .xihe.md 等（编码类专家开启）"
          checked={draft.projectContext}
          onChange={(v) => update('projectContext', v)}
        />
        <Toggle
          label="启用"
          hint="停用后不再参与任务分工（配置保留，可随时再开）"
          checked={draft.enabled}
          onChange={(v) => update('enabled', v)}
        />
      </div>
      </fieldset>
      <div className="flex justify-end gap-2 pt-1">
        <button
          onClick={onCancel}
          className="rounded-lg border border-line-strong px-3 py-1.5 text-xs text-ink-2 transition hover:bg-elevated"
        >
          取消
        </button>
        <button
          onClick={onSave}
          disabled={saving}
          className="rounded-lg bg-brand px-3 py-1.5 text-xs text-white transition hover:bg-brand/90 disabled:opacity-40"
        >
          {saving ? '写入中…' : '保存'}
        </button>
      </div>
    </div>
  )
}

/** Tracks which section is currently in view. The rootMargin band sits near the
 *  top of the viewport so the active item matches what the user is reading. */
function useScrollSpy(ids: string[]): string {
  const [active, setActive] = useState(ids[0] ?? '')
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]) setActive(visible[0].target.id)
      },
      { rootMargin: '-20% 0px -70% 0px', threshold: 0 }
    )
    for (const id of ids) {
      const el = document.getElementById(id)
      if (el) observer.observe(el)
    }
    return () => observer.disconnect()
  }, [ids])
  return active
}

function SettingsNav({ active, onJump }: { active: string; onJump: (id: string) => void }) {
  return (
    <nav className="w-48 shrink-0 overflow-y-auto border-r border-line bg-panel/50 py-3">
      {NAV.map((group, gi) => (
        <div key={group.group}>
          {gi > 0 && <div className="mx-4 my-2 border-t border-line" />}
          <div className="px-4 pb-1 text-[10px] font-semibold uppercase tracking-wider text-ink-4">
            {group.group}
          </div>
          {group.items.map((item) => {
            const Icon = item.icon
            const on = active === item.id
            return (
              <button
                key={item.id}
                onClick={() => onJump(item.id)}
                className={cn(
                  'flex w-full items-center gap-2.5 border-l-2 px-4 py-2.5 text-left text-sm transition-colors',
                  on
                    ? 'border-brand bg-brand/10 text-brand'
                    : 'border-transparent text-ink-3 hover:bg-elevated hover:text-ink'
                )}
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="truncate">{item.label}</span>
              </button>
            )
          })}
        </div>
      ))}
    </nav>
  )
}

const textCls =
  'w-64 rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30'
const numCls =
  'w-24 rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30'
const selCls =
  'w-40 rounded-lg border border-line-strong bg-elevated px-3 py-1.5 text-sm outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/30'

/** Two-column config row: title (+ optional hint) on the left, control on the
 *  right. Rows are separated by the parent's divide-y. */
function FieldRow({
  label,
  hint,
  required,
  children,
}: {
  label: ReactNode
  hint?: string
  required?: boolean
  children: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink">
          {label} {required && <span className="text-danger">*</span>}
        </p>
        {hint && <p className="text-xs text-ink-4">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

/** A labeled on/off switch rendered as a FieldRow. */
function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink">{label}</p>
        {hint && <p className="text-xs text-ink-4">{hint}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cn(
          'relative h-5 w-9 shrink-0 rounded-full transition',
          checked ? 'bg-brand' : 'bg-contrast'
        )}
      >
        <span
          className={cn(
            'absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all',
            checked ? 'left-[18px]' : 'left-0.5'
          )}
        />
      </button>
    </div>
  )
}

function Card({
  id,
  title,
  badge,
  headerRight,
  children,
}: {
  id: string
  title: string
  badge?: ReactNode
  headerRight?: ReactNode
  children: ReactNode
}) {
  return (
    <section id={id} className="scroll-mt-6 rounded-xl border border-line bg-panel/40 p-6">
      <div className="mb-4 flex items-center gap-2">
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        {badge}
        {headerRight && <div className="ml-auto">{headerRight}</div>}
      </div>
      {children}
    </section>
  )
}

function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line p-3 text-xs text-ink-4">
      {children}
    </div>
  )
}

function Dot({ on }: { on: boolean }) {
  return (
    <span
      className={cn(
        'h-2 w-2 shrink-0 rounded-full',
        on ? 'bg-success' : 'bg-strong'
      )}
    />
  )
}

function Tag({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: 'neutral' | 'sky' | 'emerald' | 'rose'
}) {
  const cls = {
    neutral: 'border-line-strong text-ink-3',
    sky: 'border-accent/50 text-accent',
    emerald: 'border-success/50 text-success',
    rose: 'border-danger/50 text-danger',
  }[tone]
  return <span className={cn('rounded border px-1.5 py-0.5 text-[10px]', cls)}>{children}</span>
}
