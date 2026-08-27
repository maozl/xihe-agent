# 当前活跃工作

> 当前进行中的需求、变更、待决问题。会话开始时优先查阅。

## 已完成：审批系统三步扩展——ask 规则 + 记忆落盘与维度 + cron 审批卡（2026-08-25，pytest 全绿）

- **ask 规则**：`approvals.ask: [write_file, patch]` 一行圈定任意工具需确认（置于 allow 之后可 carve-out）；本部署 config 已配。
- **审批记忆落盘**：`agent_home/approvals/` 按桶分文件，30 天 TTL（`memory_days` 非法值回落 30），重启/跨进程不丢。
- **记忆三维度**：普通对话按会话、定时任务按 `cron_job:{任务名}`、serve 工作空间按 `ws:{目录}`（`chat(approval_key=)` 换桶）。
- **cron 审批卡闭环（gateway）**：有 deliver 通道的任务审批卡发到目标聊天，回复整词 y/n/a 折批复（后台路由表，按实际适配器名登记；活动 turn 审批优先于 cron 挂卡）；批 "a" 落任务名桶后以后静默放行；发卡失败/无通道维持无人值守即拒；删/暂停任务直接 interrupt 解除等待。

架构订正见 [[0037_approval-permission-system]]，变更见 [[0039_ask-rules-approval-dimensions]]。**注意：`.py` 改动需重启 serve/gateway 生效；全部改动尚未 commit。**已知边界：飞书适配器不消费 steer（cron 卡回复等超时拒，企微全链路可用）；桌面 serve 的 cron 审批卡属二期。

## 已完成：危险操作审批系统（2026-08-20，pytest 全绿 + 桌面 build 绿）

terminal 死代码审批分支补全为完整系统：**三值决策管线**（`evaluate() → allow/ask/deny`，借鉴 Claude Code——deny/allow 配置规则 + 会话记忆 + always 通道）+ dispatch 单汇聚点门 + XiheAgent 阻塞等待协调（五结局保守失败）+ CLI/gateway/serve-desktop 三模式审批交互 + 桌面审批卡（批准 / **批准，不再询问** / 拒绝）。架构见 [[0037_approval-permission-system]]，变更见 [[0038_dangerous-operation-approvals]]。**注意：`.py` 改动需重启 serve/gateway 生效；cron 场景已升级为审批卡闭环（见上），仅 deliver=local/无通道的任务维持无人值守即拒（要自动跑危险命令设 `mode: auto`）。**

## 已完成：prompt 装载整改 + 系统提示词瘦身（2026-08-19，pytest 131 绿）

三轮整改：① `core/prompts.py` 从 11 个 if 链重构为声明式 `LAYERS` 表（PromptCtx + `_tool_guard`/`_passthrough` 工厂，表序=节序）+ 修 CODING_GUIDANCE 条件（读工具移入 base 后按写/执行面 `CODING_TOOLS` 判定）+ item7 delegate 半句条件化 + Memory 双节合单节；② `prompt_context.py` 修双头包裹 bug + 标题层级统一 + 路由指令去重（roster 阶梯为唯一仲裁点）；③ KBS 协议文本瘦身：`kbs_protocol.md` 2965→2294、`kbs_templates/AGENT.md` 20782→5510（-73%，删与 preamble 重复 9 节，收录 11 步/决议账本/领域专项检查逐条保留，49/49 语义不变量核验，实例 `.biz_kbs` 已同步）。prompt 装载逻辑与三层 agent 差异沉淀为 [[0036_system-prompt-assembly]]。**注意：`prompts.py`/`prompt_context.py` 为 .py 改动，运行中的 serve/gateway 需重启生效；两份 .md 即时生效。**

## 已完成：三层 agent 名单统一 + specialists.enabled 闸门（2026-08-17，全验证绿）

主/专家/delegate 三层名单收口：主 agent 从 **config.yaml 顶层 `toolsets`/`skills`** 实例化，与专家共用一个 `resolve_roster`（删全部 main 专属逻辑）；统一三态语义（不写/`[]`=不加载、`["*"]`=全量、名单=白名单）；`specialists.enabled` 总闸默认关；agent.py `is not None` 修复（`[]` 曾被 truthiness 翻成全量）；serve `_capabilities` 按主名单收敛；桌面能力开关卡新增专家委派 Toggle + SpecialistsCard「未开启」横幅。架构见 [[0034_three-layer-agent-roster]]，变更见 [[0035_three-layer-roster-unification]]。验证：pytest 41 + 三层 walkthrough + `_capabilities` 三态实测 + 桌面 build 绿。**破坏性提醒：存量部署不写 `toolsets` = 主 agent 无工具、不写 `specialists.enabled` = run_*_agent 消失；运行中 serve/gateway 需重启。**

## 已完成：专家 agent 落地 + 工具集目录重构（2026-08-16，全验证绿）

配置声明的常驻专家系统完成：`agents/<slug>.yaml` 每专家一文件（连接键留空继承主配置）+ serve CRUD（api_key 永不回显）+ 桌面 SpecialistsCard 编辑器（工具集/MCP 按服务器/技能白名单 chips、待重启徽标）+ skills「不选=不注入任何技能」空集语义 + `mcp-<server>` 按需授权。**工具集目录重构**为 14 个带中文标签的平铺组（删组合预设/includes/browser_scripts、core 四拆、agent 拆出 skills）。架构见 [[0032_specialist-agents]]，变更记录见 [[0033_specialist-toolset-overhaul]]。验证：e2e 28 项 + pytest 12 + 桌面 build 全绿；用户机 itsm.yaml 已迁移。**注意：运行中的 serve/gateway 需重启才切到新组名。**

## 进行中：xihe-desktop 桌面端做完整（F1）

定调见 [[0026_desktop-agent-model-built-in-xihe]]（2026-08-11）：**Agent = 类型**——xihe 桌面**内置**（main 进程托管 `xihe serve` 生命周期）、claude **可添加**（connector 占位，本阶段不实现）；**非多实例 / 非 persona**。Workspace = 项目文件夹 / 用户资产（与 agent 正交，一等公民）。Manage 范围**待定**（用户晚点确认；现状 ManagePanel 已只读接 MCP/skills/cron）。

**F1 工作流**（下一步实现，单独出计划）：
1. main 托管 `xihe serve` 子进程生命周期（spawn / health / restart / cleanup）+ main→renderer `xihe:status` IPC。
2. 花名册重构：`SEED_AGENTS`（3 demo）→ 单一内置 xihe（live）+ claude 可添加占位（IA 槽 disabled）；删 `xihe-research` persona 假槽。
3. 去 "serve" 措辞：UI / 文案不再暴露 "serve"（连接状态、demoReply、空态提示）。

## 已完成：F2 升级 — ClaudeRunner 长驻 stream-json 重写（2026-08-12，双路径实测通过）

F2（claude 接入 + 统一 LLM 配置）的性能/复用升级：`ClaudeRunner` 从「每轮新进程 + `--resume`」→「一会话一长驻进程，stdin 跨轮喂 NDJSON」。**代码完成 + tsc/build 绿 + 热/冷双路径实测通过**。详见正式 Change [[0027_desktop-claude-longlived-rewrite]]。

**生命周期健壮性（2026-08-12，已落地，tsc/build 绿）**：长驻重写后实测暴露 **stdin-boot 死锁**（claude `--input-format stream-json` 须先收 stdin 首行才 boot；旧 `send` 把首轮 buffer 到 `pendingSend` → 双向死等 → 45s 超时 → 停止按钮 no-op，**非瞬态**）。修：① send 冷启直接 `writeTurn` ② interrupt 无条件 teardown（覆盖 boot 窗口）③ 45s 就绪超时 `READY_TIMEOUT_MS` ④ 10min 空闲回收 `IDLE_TTL_MS` ⑤ PID 文件 + `sweepClaudeOrphans()` 异常关闭孤儿清扫（wmic 指纹校验防 pid 复用误杀）。详见 [[0028_desktop-claude-transport-architecture]] 生命周期表 + [[0029_desktop-dual-engine-architecture]] 硬化节。残余：协议级 interrupt / 权限审批 UI / app 内端到端 happy-path（建议 `npm run dev` 走一遍）。

**架构总览沉淀（2026-08-12）**：新增 [[0029_desktop-dual-engine-architecture]] —— 桌面端整体架构 hub（双引擎并列、统一 `ServeEvent` 归约、main 总编排、两路传输对照、凭据与真理归属），串联 0024/0025/0026/0027/0028。

> 中途插曲：claude 自动升 2.1.228 postinstall 损坏（bin/claude.exe 变 500 字节桩），阻塞了冷 resume 验证；用户更新到可工作版本后完成验证。已记入自动记忆。

[[0025_desktop-control-plane]] 的 Agent 层建模（persona / 3 种子 agent / serve 显式暴露）已据 [[0026]] 就地订正；该页其余（控制面定位 / 能力驱动 UI / 三段式 / store / 协议引用）仍有效。

## 知识库现状（2026-08-26）

- 已收录 40 个正式页面（1 entity / 27 concept / 10 change / 2 insight）+ 6 个原始快照。
- [[0040_external-agent-adapter-protocol]] —— **外部 agent 适配器协议**（2026-08-26，候选提升）：claude/codex 双引擎驱动（WARM 长驻 / ONE-SHOT）+ 协议对照 + 平台硬坑 + IPC 决策；与 [[0028]] 组双页。
- [[0037_approval-permission-system]] + [[0038]] + [[0039]] —— **审批与权限系统**（2026-08-20 首版 + 08-25 扩展）：三值决策管线（ask 规则 + 记忆落盘三维度 + cron 审批卡闭环）+ dispatch 单门 + 三模式审批交互；启发式非安全边界。
- [[0034_three-layer-agent-roster]] + [[0035_three-layer-roster-unification]] —— **三层 agent 名单模型**（2026-08-17）：主=顶层键/专家=agents/*.yaml+总闸/delegate=运行时三态，主/专家共用 `resolve_roster`；`[]` vs `None` 不变式；`_capabilities` 收敛。
- [[0032_specialist-agents]] + [[0033_specialist-toolset-overhaul]] —— **专家 agent 系统**（2026-08-16）：配置声明常驻专家 + serve CRUD + 桌面编辑器 + 工具集 14 平铺组重构；[[0017]] 角色化的后继（机制对照）。
- [[0026_desktop-agent-model-built-in-xihe]] —— **桌面端 Agent 模型定调**（insight/ADR）：Agent = 类型（内置 xihe + 可添加 claude），非多实例 / 非 persona；推翻 [[0025]] 的 Agent 层建模，驱动 F1。
- [[0024_desktop-serve-protocol]] + [[0025_desktop-control-plane]] 记录了 xihe 的**第三运行模式 `xihe serve`**（HTTP+WS 服务）与**桌面控制面**（独立仓 xihe desktop，经协议驱动 serve）。桌面端 P0（serve 接入 `xihe-ops` 槽）已落地；**P1 persona 已废弃**、P2 claude 重构为「可添加 agent 类型」、P3 管理 UI 的 MCP/skills/cron 已只读接入、P4 CodeBuddy 仍是枚举值（roadmap 在 [[0025]]，按 [[0026]] 订正）。
- [[0027_desktop-claude-longlived-rewrite]] + [[0028_desktop-claude-transport-architecture]] 记录了 **claude 长驻 stream-json 重写**（一会话一长驻子进程 + stdin 跨轮喂 NDJSON；冷/热双路径实测通过）。[[0029_desktop-dual-engine-architecture]] 是**整体架构 hub**——双引擎并列（serve WS + claude STDIO）、统一 `ServeEvent` 归约、main 总编排、两路传输对照、凭据与真理归属；含 ClaudeRunner 生命周期健壮性 5 项（stdin-boot 死锁修复 + interrupt 无条件 teardown + 45s 就绪超时 + 10min 空闲回收 + PID 孤儿清扫）。

## 后续方向

- **角色化已回退**（2026-07-22，见 [[0017]]/[[0018]]）：方案设计问题（主 agent 路由负担/隔离副作用/绑定注入 token/边界模糊），回归单 agent + request_tools + skill_view + ad-hoc delegate。**2026-08-16 后继**：专家 agent（[[0032]]）以不同机制（配置声明派生工具 + 白名单收口）重新落地常驻专家。
- 待推进：A2A 对外暴露（Phase 2 server / Phase 3 client，复用 a2a-sdk）—— 独立于角色化，仍可做。
- **【已落地】外部 agent 接入（claude/codex 作能力补充）**（2026-08-12 立项 → 2026-08-26 实测+落地，正式参考页 [[0040_external-agent-adapter-protocol]]，与 [[0028]] 组双页）：codex headless 协议与 claude 同构（NDJSON + 冷 resume + cwd + env），**通用 external-agent adapter 成立并落地内核**（`core/external_agent.py` 共享机制层 + ClaudeDriver(WARM)/CodexDriver(ONE-SHOT)；接入模式已定 IPC 不走 MCP；网关 Responses API 直连可用，代价 = `--disable multi_agent` 必带；Windows sandbox 用 unelevated；测试 344 绿 + E2E 两轮含 resume 记忆）。剩余开放（见 0040 开放点节）：能力共享边界（外部引擎用 xihe skill/MCP）、app-server 长驻优化（无痛点不动）。是产品目标「CLI+桌面双模式 agent，自研能力不足时接入 claude/codex」（[[0026]] 「可添加 agent」的延伸）的传输层。
- **【待办】桌面思考块（thought_delta）对 glm-5.2-zp 休眠**（2026-08-10，见 [[0025_desktop-control-plane]]）：直连 GLM 网关（`<内部网关IP>/public/v1`）原始 SSE 探针确认——glm-5.2-zp **默认调用不发** `reasoning_content`/`reasoning`/`thinking` 任一字段（204 chunk 的 delta 只有 `content`+`role`），推理写在 `content` 里。非 agent 读错字段名、非网关剥离。桌面已实现的 `thought_delta` 渲染（`store.handleEvent` + `TurnTrace` 组件）逻辑正确但**无数据可渲染**。→ 解法二选一：① 换带独立 reasoning 通道的模型（thinking 变体 / DeepSeek-R1 类）；② 验证 glm-5.2-zp 加 `thinking` 参数是否支持（未测，内部 `-zp` 变体大概率不支持）。`agent.py:1008` 的 `reasoning_content` 读取代码保留，换模型即自动生效。

- **【待办/设计参考】xihe+desktop 打包发行**（2026-08-13，见 [[0030_packaging-distribution-strategy]]）：桌面端已迁入 `xihe-agent/desktop/`。**Windows** 可行（Electron + 冻结 `xihe serve` 子进程，electron-builder 出 NSIS；卡点=本地 bundle Python(PyInstaller/嵌入式) + paddle 重原生 + 首跑数据目录/ConfigPanel 引导 + Authenticode）；**macOS** 同框架但需 mac 构建机（不能交叉构建）+ 签名/公证（气隙做不了完整 notarization → ad-hoc + quarantine 或 MDM）+ ⚠️ 验证 paddle mac arm64 wheel + deep-sign python 树；**iOS/手机不能同款**（Electron/Python 子进程都不上 iOS）→ 唯一形态=瘦客户端连远程 serve/gateway（复用 WS 协议），App Store 政策禁工具调用 agent。**手机用 xihe 的现成路径 = gateway 模式**（在企微/飞书里跟 bot 说话，server 跑 agent），无需新打包。打包本质=决定 Python 大脑跑哪（本地 bundle vs 远程），serve 协议是 enabler。建议顺序：先 Windows → macOS → iOS/移动另立项。

## 待决问题

_暂无_

---

最近更新见 [recent.md](recent.md)；页面索引见 [index.md](index.md)。
