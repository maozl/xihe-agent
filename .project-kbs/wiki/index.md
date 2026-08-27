# 项目 Wiki 索引

> 按内容分类的页面与来源目录。新增 / 更新页面时在此登记。

本知识库遵循 [PROTOCOL.md](../PROTOCOL.md) 的工作流与模板。

## 状态

- 初始化日期: 2026-07-01
- 健康度: [meta/lint-status.json](../meta/lint-status.json)
- 页面数: 39 · 原始快照: 6

## Concepts (概念 / 规范)

- [0002 工具注册表与调用链](concepts/0002_tool-registry-and-dispatch.md) — ToolRegistry 自注册 + dispatch + toolset + AuxiliaryClient + 子 agent + 三层上下文防御
- [0003 浏览器工具集](concepts/0003_browser-tools.md) — Playwright、a11y tree + ref ID、**CDP 托管默认** + persistent 兜底、browser_logout
- [0004 上下文压缩机制](concepts/0004_context-compression.md) — 4 步算法、tail 保护、增量摘要
- [0005 MCP 动态工具注册](concepts/0005_mcp-dynamic-registration.md) — 不用中间代理、常驻连接、后台 event loop
- [0006 会话两层 ID 设计](concepts/0006_session-design.md) — session_key/session_id、source-only API、重置策略
- [0007 Skills 系统](concepts/0007_skills-system.md) — progressive disclosure 三层、SKILL.md 格式
- [0009 定时任务（cron）设计](concepts/0009_cron-jobs.md) — 60s tick 无状态基线 + job 三形态（prompt/纯脚本/脚本喂prompt）+ wake gate + context_from
- [0011 Gateway 架构](concepts/0011_gateway-architecture.md) — SharedContext + 每消息新建薄 agent + SQLite 续对话 + 阻塞 join 并发模型
- [0013 Toolset Scope 与按需展开](concepts/0013_toolset-scope-and-dynamic-expansion.md) — 默认裁剪 + request_tools 元工具按需展开 web/media/scheduler + 设计决策（为什么不用向量检索/LLM 分类）。⚠️ 目录表已过时：2026-08-16 重构为 14 平铺组（core 四拆/includes 删除），组目录以 [[0033_specialist-toolset-overhaul]] 为准
- [0014 项目上下文加载规则](concepts/0014_project-context-loading.md) — .xihe.md/AGENTS.md 始终加载 + CLAUDE.md/.cursorrules 可配置 + 全部合并(不是 first-match-wins)
- [0016 中断/停止/Steer 控制通道设计](concepts/0016_interrupt-stop-steer.md) — 停止走带外控制通道(绕过队列) + per-agent contextvar 中断 + interruptible_iter(循环)/子进程注册表(子进程) + steer(自然结束转新轮/停止丢弃) + 7 个演进坑
- [0017 角色化子 agent（Roles）](concepts/0017_role-based-subagents.md) — ⚠️ **已废弃**（角色化回退）预定义角色 + delegate_task(role=...) + skill索引精简 + MCP分组
- [0018 工具/技能/角色分层架构](concepts/0018_tool-skill-workflow-role-layering.md) — ⚠️ **角色层已废弃**（回退），tool/skill 两层 + workflow是skill编排用法 + 关系图
- [0022 测试策略——分层模型](concepts/0022_testing-strategy.md) — L0-L4 分层 + 注入假模型 client(`FakeChatClient`)测循环不变量 + 测试隔离约定 + 行业对照；`tests/` 12 测试已落地
- [0023 多实例配置——`--config` 启动时选实例](concepts/0023_multi-instance-config.md) — 一个 YAML 描述一个实例(`agent_home`→数据根隔离) + peek argv 设计(0 下游重构)。⚠️ 优先级分层/`.env` 浮顶陷阱部分已过时（2026-08-13 起配置单源：无 `.env`/env 覆盖/`${VAR}` 展开），以页首订正注记与 [[0001_xihe-agent]] 为准
- [0024 桌面端通信协议与 xihe serve 模式](concepts/0024_desktop-serve-protocol.md) — 第三运行模式 `xihe serve`(aiohttp HTTP+WS,复刻 gateway 内核但 `run_in_executor` 解放循环) + REST/WS 契约 + Emitter 跨线程桥(stdlib queue) + 会话映射 platform=serve + 能力描述符 + 中断/掉线必中断
- [0025 桌面端控制面设计](concepts/0025_desktop-control-plane.md) — 独立仓 xihe desktop(Electron,不内嵌引擎,经协议驱动 serve) + 三层模型 Provider/Agent/Session + 两 provider 形态(process/connector) + 能力驱动 UI + LIVE_SLOT 升级 + Roadmap P0-P4。⚠️ **Agent 层（persona / 3 种子 agent / serve 显式暴露）已被 [[0026]] 订正**：改为内置 xihe + 可添加 claude；P1 persona 废弃。控制面 / 能力驱动 / 三段式 / store 仍有效
- [0028 桌面端 claude 接入架构](concepts/0028_desktop-claude-transport-architecture.md) — claude = 第二种 agent 引擎，**一会话一长驻子进程 + stdin 跨轮喂 NDJSON**；stdout NDJSON → 同形 ServeEvent → 复用 renderer 归约；冷/热双路径（进程死后 `--resume` 续同会话）实测通过；interrupt/dispose 语义分离；凭据 spawn 时注入（env+`--model`，api_key 永不外泄）
- [0029 桌面端双引擎架构总览](concepts/0029_desktop-dual-engine-architecture.md) — **整体架构 hub**：xihe serve（WS）+ claude（STDIO）两引擎并列，统一 `ServeEvent` → renderer 引擎无关归约；双引擎总览图 + 两路传输对照表（进程/并发/中断/真理归属/凭据）+ main 总编排（ServeSupervisor adopt-or-spawn + ClaudeRunner 长驻 + IPC 桥）+ 凭据与会话真理归属 + 按角色分流阅读路径。串联 0024/0025/0026/0027/0028
- [0030 xihe+desktop 打包发行策略](concepts/0030_packaging-distribution-strategy.md) — **设计参考（未实现）**：Windows 可行（Electron + 冻结 `xihe serve` 子进程，electron-builder 出 NSIS） / macOS 同框架但需 mac 构建机 + 签名公证（气隙做不了完整 notarization）+ ⚠️ paddle mac arm64 待验证 / **iOS（手机）不能同款**（Electron/Python 不上 iOS）→ 瘦客户端连远程 serve/gateway。**手机用 xihe 的现成路径 = gateway 模式**（企微/飞书跟 bot 说话）。打包本质=决定 Python 大脑跑哪（本地 bundle vs 远程），serve 协议是 enabler
- [0031 工作空间 cwd 绑定与入口差异](concepts/0031_workspace-cwd-binding.md) — cwd 绑定在会话上（桌面私有 convWorkspace map）、**随每轮 sendTurn 传 serve、不落库**；入口无关（外层列表=空间内）；CLI 完全独立（会话不可见+进程 cwd）；失效边界 3 条；「历史消息 ≠ 运行时 cwd」
- [0032 专家 Agent](concepts/0032_specialist-agents.md) — 配置声明的常驻专家：`agents/<slug>.yaml` 一文件一专家 → 派生 `run_<slug>_agent` 工具 + 花名册路由；完整分层 prompt（vs delegate wholesale 覆盖）；连接键留空继承；skills 白名单 **None=全量索引 / 空集=不注入**（falsy 翻转陷阱）；`mcp-<server>` 按需授权（get_schemas 双路匹配）；api_key 永不回显（api_key_set 布尔 + PUT 三态）；与已废弃角色化 [[0017]] 的差异对照。⚠️ toolsets 缺省语义 + `specialists.enabled` 总闸已被 [[0034]] 订正
- [0034 三层 Agent 名单模型](concepts/0034_three-layer-agent-roster.md) — **主/专家/delegate 名单收口**：主 agent = config.yaml 顶层 `toolsets`/`skills`（与专家共用 `resolve_roster`，无 main 专属逻辑）；统一三态（不写/`[]`=不加载、`["*"]`=None 全量、名单=白名单且 `mcp-<server>` 永远保留）；**`[]` vs `None` 不变式**（truthiness 反转陷阱，agent.py `is not None`）；`specialists.enabled` 总闸默认关；delegate 运行时三态独立于父 + `subagent_blocked` 12 类；load_config 白名单两循环陷阱；serve `_capabilities` 按主名单收敛；三次设计纠正记录（终态=主 agent 就是根配置）
- [0036 系统提示词装载与三层 Agent prompt 差异](concepts/0036_system-prompt-assembly.md) — **prompt 侧对偶 [[0034]]**：声明式 `LAYERS` 表（PromptCtx + `_tool_guard`/`_passthrough` 工厂，表序=节序，`expand_agent_vars` 收尾）+ 四层组 18 层注入条件表 + **三层三条装载路径**（主=完整层组含 platform/kbs preamble/roster/记忆快照；专家=同一条装配代码换 persona 入参、无 platform/preamble/roster 层但指导层按自身工具面照常裁剪；delegate=`system_prompt_override` 三处短路→纯任务卡无任何指导层）+ 不变式（条件层 key off ctx.tools 且与 chat loop 同过滤器、CODING_TOOLS 按写/执行面判定、记忆快照不进 prompt 文本、.md 即时生效 vs .py 需重启）
- [0037 危险操作审批与权限系统](concepts/0037_approval-permission-system.md) — **三值决策管线（借鉴 Claude Code）**：`evaluate() → allow/ask/deny`（优先级 mode auto > deny 规则 > allow 规则 > 审批记忆 > ask 规则 > 危险判定 > LLM 语义判定）；四层架构（判定 `_approvals.py` 纯函数 / 协调 `_approval_shared` 阻塞等待 / 拦截 dispatch 单门 / 通道三模式+cron 后台审批卡）；规则语法 `"tool(glob)"`（terminal/ssh_exec 匹配命令原文，write_file/patch 取路径，**无 action 工具只能整名覆盖**）；**ask 规则**（config 圈定任意工具需确认，置于 allow 后可 carve-out）；**审批记忆落盘 30 天 TTL** + 三维度桶（会话 / `cron_job:任务名` / `ws:目录`，`chat(approval_key=)` 换桶）；五结局状态机保守失败；**启发式非安全边界**（变量间接可绕过）
- [0040 外部 agent 适配器协议（claude + codex 双引擎驱动）](concepts/0040_external-agent-adapter-protocol.md) — xihe 内核侧把本地 claude/codex CLI 作可委派外部引擎（`run_external_agent`）：协议同构（NDJSON + 会话 id 冷 resume + cwd + env 凭据 + 杀进程中断）→ 一个适配层两策略（**ClaudeDriver WARM 长驻 stdin 跨轮 / CodexDriver ONE-SHOT 每轮新进程 + `exec resume`**）；`(engine, session_key)` 双键 resume 表；**stdin 语义两引擎方向相反**（claude 首行 boot / codex EOF 才跑）；协议对照表 + codex 事件映射 + 平台硬坑（`--disable multi_agent` 必带——litellm namespace 500 被包成 "high demand" 误导；Windows sandbox 用 unelevated）；已决 IPC 不走 MCP；与 [[0028]] 组双页。双引擎均实测通过

## Entities (实体)

- [0001 xihe-agent 项目总览](entities/0001_xihe-agent.md) — 单进程 OpenAI 兼容工具调用 agent，CLI + 网关双模式

## Stories (需求 / 故事)

_暂无_

## Changes (代码变更)

- [0010 浏览器 CDP 默认 + cron job 多形态 + 提示词约定](changes/0010_cdp-default-and-cron-job-forms.md) — 三组已验证落地改动（2026-07-03）
- [0012 新增 http/maven/node 工具 + web 门控 + gateway 增强](changes/0012_new-tools-and-gateway-enhancements.md) — 新工具 + web check_fn 门控 + gateway/file/mcp/cron 增强（2026-07-09）
- [0019 KBS 子系统——可插拔业务知识库协议](changes/0019_kbs-feature.md) — **功能点**:总开关 kbs.enabled 门控前导+工具(check_fn)、关掉零足迹;kbs_init/status/search + 复用 core 文件工具(2026-07-30)
- [0020 会话管理命令](changes/0020_session-management-commands.md) — **功能点**:`/sessions`(隐藏内部+按用户过滤)/`/history [N]`/`/resume`(会话内切换)/`xihe chat -r` + `list_sessions` 后端(2026-07-30)
- [0021 CLI hybrid TUI](changes/0021_cli-hybrid-tui.md) — **功能点**:hybrid REPL(空闲 prompt-toolkit + 处理中 msvcrt steer)、cwd 注入、fresh-by-default、来晚 steer 自动续跑、CODING_GUIDANCE 7 点、日志 file_level、新依赖 prompt_toolkit+textual(2026-07-31)
- [0027 xihe-desktop ClaudeRunner 长驻 stream-json 重写](changes/0027_desktop-claude-longlived-rewrite.md) — **重构**:每轮新进程 → 一会话一长驻进程(stdin 跨轮喂 NDJSON)，省 ~5s 启动 + 复用 prompt-cache；热路径(stdin 多轮) + 冷路径(进程死后 `--resume` 续同会话)双路径实测通过；新增 dispose 补 deleteConversation 缺口(2026-08-12)
- [0033 专家 Agent 落地 + 工具集目录重构](changes/0033_specialist-toolset-overhaul.md) — **功能点**:agents section → 每专家一文件 + serve CRUD + 桌面编辑器(chips/待重启徽标)；skills「不选=不配置」空集语义修复；工具集 14 平铺组(删组合预设/includes/browser_scripts、core 四拆、agent 拆 skills)；itsm.yaml 迁移；e2e 28 项 + pytest 12 + 桌面 build 全绿(2026-08-16)
- [0035 三层名单统一 + specialists.enabled 闸门](changes/0035_three-layer-roster-unification.md) — **重构**:主 agent 从 config.yaml 顶层键实例化(删全部 main 专属 resolver)+ agent.py `is not None` 修复(`[]`曾被翻成全量) + config 白名单两循环扩展 + `_capabilities` 按主名单收敛 + delegate schema blocked 清单修正(5→12) + 桌面专家委派 Toggle/横幅；破坏性:不写 toolsets=无工具、不写 specialists.enabled=run_*_agent 消失(2026-08-17)
- [0038 危险操作审批落地](changes/0038_dangerous-operation-approvals.md) — **功能点**:三值权限管线(evaluate allow/ask/deny + deny/allow 规则 + 会话记忆) + dispatch 单汇聚点门 + XiheAgent 阻塞等待(五结局保守失败) + 子代理共享 `_approval_shared` + CLI/gateway/serve 三模式审批交互(桌面审批卡含「批准，不再询问」第三按钮) + `_DANGEROUS_PATTERNS` 30 条正则从 terminal.py 移入 `_approvals.py` + 高危参数表 7 类；terminal 死分支删除(2026-08-20)
- [0039 ask 规则、审批记忆落盘与维度、cron 审批卡闭环](changes/0039_ask-rules-approval-dimensions.md) — **功能点**:`approvals.ask` 通用圈定(任意工具加行即拦,allow 可 carve-out) + 记忆落盘 `agent_home/approvals/`(按桶分文件/30 天 TTL/原子写/滑动续期,`memory_days` 非法回落 30) + 记忆三维度(`approval_key` 换桶:cron 按任务名/serve 工作空间按目录) + cron 审批卡闭环(卡片投 deliver 聊天→后台路由表折 y/n/a→"a" 落任务名桶以后静默放行;发卡失败即拒;删/暂停任务 interrupt 解除等待;无通道维持无人值守即拒);飞书 steer 缺口与桌面 serve cron 卡二期为已知边界(2026-08-25)

## Insights (决策 / 踩坑)

- [0009 Agent 安全——主人身份与私密信息](insights/0009_agent-security-master-identity.md) — prompt injection vs 代码层硬控制、CaMeL/Claude Code/商用方案、xihe 推荐方案（路径黑名单+脱敏+chat_id 绑定+来源标注）
- [0026 桌面端 Agent 模型定调——内置 xihe + 可添加 claude](insights/0026_desktop-agent-model-built-in-xihe.md) — Agent=类型(内置 xihe 由 main 托管 serve 生命周期 + 可添加 claude connector 占位)，非多实例/非 persona；推翻 [[0025]] Agent 层建模，驱动桌面 F1

## Candidates (候选 / 待验证)

> 见 [meta/candidates/index.md](../meta/candidates/index.md) — 暂无开放候选。

## 原始材料快照 (raw/sources)

- [browser-tools.md](../raw/sources/browser-tools.md)
- [context-compression.md](../raw/sources/context-compression.md)
- [mcp-dynamic-registration.md](../raw/sources/mcp-dynamic-registration.md)
- [session-design.md](../raw/sources/session-design.md)
- [skills.md](../raw/sources/skills.md)
- [tool-design.md](../raw/sources/tool-design.md)
