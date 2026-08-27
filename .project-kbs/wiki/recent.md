# 最近更新

> 最近新增、更新、修正或已过时的知识。按时间倒序。

## 2026-08-26

- **落地（codex base_url 透传 + extra_args）** [[0040]] — `external_agents.codex.base_url` 设置 → CodexDriver 发五连 `-c` 内联定义 provider `xihe`（env_key=CODEX_API_KEY、`wire_api` 默认 responses 非法回落），**opt-in 不回退主 base_url**（防止悄悄改写 config.toml 选好的 provider）；值原样透传不剥 `/v1`（codex 自拼 /responses|/chat/completions，与 claude 的剥 /v1 相反）。新增 `extra_args`（list）逃生口：原样追加、各引擎 resume 旗标之前（codex resume/"-" 前；claude --resume 前，两引擎通用；`-c` 键值对、`--ephemeral`、`--settings`…；`-c` 值按 TOML 解析、普通字符串可不加引号）。测试 +4（五连 -c/extra_args 位置/creds opt-in/claude 位次），pytest 348 绿；真实 E2E 两发（内联 provider 覆盖 config.toml 的 zp、--ephemeral 旗标位）。0040 argv 契约块/对照表/协议要点/落地节同步。.py 改动需重启 serve/gateway。
- **订正 Concept + 代码注释（codex `-c` 配置覆盖通道，用户带来外部说明要求验证）** [[0040]]/`external_agent.py`/`external_agent_tool.py`/`config.example.yaml` — 本机 0.146.1 实测证实：`codex exec -c key=value`（value 按 TOML 解析、失败回落原始字符串）可覆盖 `model_provider` / `model_providers.<id>.base_url|env_key|wire_api`；用 config.toml 里**不存在**的 provider id 纯 `-c` 定义并打内部网关，一轮真实完成（`CLI-OVERRIDE-OK` + `turn.completed`）；`--oss` / `--local-provider` / `-p/--profile` / `--ignore-user-config` / `--ephemeral` 亦在。**订正**此前「base_url 只认 config.toml」的表述：env 确实不吃（该半句成立），但 CLI 覆盖通道存在——xihe 只注入 api_key 是**实现选择非能力限制**，注释/wiki/config.example 五处改口径。
- **新增 Concept（候选提升）** [[0040_external-agent-adapter-protocol]] — **外部 agent 适配器协议（claude + codex 双引擎驱动）**，由候选 `external-agent-adapter-protocol`（2026-08-12 立项 → 08-26 实测通过）提升，与 [[0028]] 组「外部 agent 接入协议」双页（本页 = xihe 内核侧，0028 = 桌面端 claude 传输层）。沉淀稳定参考：协议同构前提（NDJSON + 会话 id 冷 resume + cwd + env 凭据 + 杀进程中断）→ 一个适配层两策略（ClaudeDriver WARM 长驻 stdin 跨轮 / CodexDriver ONE-SHOT 每轮新进程 + `exec resume`）；共享机制层（spawn 硬化/.cmd 路由/按引擎指纹孤儿清扫/`(engine, session_key)` 双键 resume 表防跨引擎串线）；codex 事件→xihe 事件映射表（`file_change` 须立即补 tool_result、`Reconnecting...` 只挂起不终局）；**stdin 语义两引擎方向相反**（claude 首行 boot vs codex EOF 才跑）；协议对照表 13 维；平台硬坑固化（`--disable multi_agent` 必带 + Windows sandbox unelevated）；已决 IPC 不走 MCP（四条理由）；开放点（能力共享边界 / app-server 长驻）随页转入。候选闭环 promoted；total_pages 39→40。
- **候选大幅更新（外部 agent 适配器协议，codex 实测通过 + 双引擎驱动落地）** `meta/candidates/external-agent-adapter-protocol` — 2026-08-12 遗留的最大风险项「内部网关 Responses API 兼容性」**实测消除**（网关为 litellm，Responses-API 请求全通：SSE/store:false/加密 reasoning/function tools/web_search/resume 时 prompt caching 命中，无需 wire_api="chat"）。实测挖出两个原调研未见的硬坑并修复：① **`--disable multi_agent` 必带**——codex 0.146 默认携带 `{"type":"namespace","name":"multi_agent_v1"}` 工具，litellm 返回 500 "Unsupported tool type: namespace"，而 codex 把所有可重试错误包成误导性的 "We're currently experiencing high demand"（mitm 抓包+重放定位）；② **`[windows] sandbox="elevated"` 需 SeBatchLogonRight**（CreateProcessWithLogonW 报 1385，所有 shell 失败）→ `unelevated` 可用。协议校订：stdin 是 **EOF-boot 门**（prompt 在 argv 时管道 stdin 仍阻塞等 EOF，与 claude 的 stdin-boot 死锁方向相反）；`exec [flags] resume <tid> -` 同样 stdin 读 prompt；`-s` 枚举仅三值（**无 bypassPermissions**，绕过走独立旗标，xihe 作跨引擎别名映射）；base_url 只认 `~/.codex/config.toml` 不吃 env。**落地**：`core/external_agent.py` 重构为共享机制层（spawn 硬化/.cmd 路由/PID 按引擎指纹清扫/`(engine, session_key)` 双键 resume 表）+ ClaudeDriver(WARM)/CodexDriver(ONE-SHOT) 两策略；`external_agent_tool.py` 双引擎泛化（codex 项目约定文件 = AGENTS.md）；新测试 `test_external_agent_codex.py` 11 项 + 全量 pytest 344 绿 + 真实 E2E 两轮（shell 执行 + resume 记忆）。~~待提升为正式 concept~~（→ [[0040]]）；开放点：能力共享边界（外部引擎用 xihe skill/MCP）。

## 2026-08-25

- **新增 Change + 订正 Concept（审批系统三步扩展）** [[0039_ask-rules-approval-dimensions]] / [[0037]] — **ask 规则 + 审批记忆落盘与三维度 + cron 审批卡闭环**：① `approvals.ask` 通用圈定（任意工具加一行即拦无需改码；**置于 allow 之后** → allow 规则可 carve-out；`_generic_summary` 从常见参数名拼摘要；always 记 `ask:{rule}` 键同规则本维度免问；`rule_text` 对 write_file/patch 反斜杠→正斜杠否则 Windows 路径匹配不上 fnmatch）。② 记忆落盘 `agent_home/approvals/`（**按桶分文件**——多进程共用 agent_home 单文件互踩；懒水合 + TTL 过滤 + tmp/os.replace 原子写 + 活跃桶滑动续期 + 每进程一次 mtime 清扫 + 坏文件 fail-open 宁可重问；`memory_days` **默认 30、非法值含 0/负数回落 30** 无 0 开关；删死代码 `needs_approval()`）。③ 三维度桶（`chat(approval_key=)` 只换审批桶不动会话键，仍只有顶层 chat 写键子代理共享读）：cron = `cron_job:{任务名}`（按名称非 id，跨运行跨进程共享）、serve 工作空间 = `ws:{正斜杠+lower 目录}`（同空间所有对话共享，桌面端零改动）。④ cron 审批卡闭环（gateway）：有 deliver 通道的任务把审批卡发到目标聊天 + **后台路由表** `_pending_external[(实际适配器名, chat_id)]`（全路径检查发现的坑：按 deliver 里写的平台名登记，`platform:chat_id` 形式与回复通道对不上 → 永远等不到折批复）→ gateway `_handle_steer` 在活动 turn 折批复之后折整词 y/n/a 给最新一张卡（优先级：活动 turn 审批 > cron 挂卡）→ 批 "a" 落任务名桶以后静默放行；发卡失败 request_cb 抛错立即拒；删/暂停任务 `_interrupt_job_run` 直接 interrupt 解除审批等待；deliver=local/无 adapter（serve 进程）维持无人值守即拒。**已知边界**：飞书不消费 steer handler（历史缺口，cron 卡回复等超时拒）、桌面 serve cron 审批卡二期（需 WS adapter + 桌面 UI）。顺带 clarify_tool 三项清理。测试：ask 规则 6 + 落盘 5 + 路由表 4 + approval_key/ws 键 2 + 新文件 test_cronjob_approval.py 7；pytest 全绿。total_pages 38→39。

## 2026-08-20
- **新增 LLM 语义判定层（审批召回增强）** [[0037]]/[[0038]] — 用户提出「正则枚举没法覆盖所有，加一层 LLM 判定」，裁决落地+默认开。正则只认已知形式（改写绕过实证在前），语义层补召回：`evaluate` 尾部对正则漏网的 terminal 命令做辅助模型复核（`agent.aux`，dispatch 注入）。关键设计：**`_SUSPECT_RE` 漏斗**（删除/破坏动词、提权、远程执行、系统区域才判——日常命令零成本）+ **固定类别枚举即类键**（`danger:llm:{category}`，「不再询问」与类记忆语义对齐，thread-local 传 remember_rule）+ **fail-open**（未接 aux/失败/解析失败放行+日志）+ **防注入**（prompt 命令当纯数据）+ **防审批疲劳**（三档评分 safe|warning|dangerous，**仅 dangerous 弹审**，warning/safe 放行记日志；拿不准判 warning）+ **反混淆**（不看字面关键词、还原编码/变量/换行混淆的真实意图；prompt 二轮采纳用户稿的 effect 字段/三档规则/反混淆行，effect=命令实际作用进审批摘要）。`approvals.llm_judge` 默认开、`auxiliary.approval_judge.model` 可换模型。测试 +8，pytest 229 绿。
- **订正 Concept + Change（会话记忆类级化 + 拒绝后改写绕过补网）** [[0037]]/[[0038]] — 两个同日实测问题：①「批准，不再询问」后删下一个文件又问——初版记忆精确匹配整条命令文本，而模型每条命令内嵌不同目标名，等于每条重问 → 升级**类记忆**（`_danger_detail` 抽出危险类键：terminal=命中模式描述/高危表=工具名；`remember_rule` 记精确文本+`danger:类键`双键；同类换目标本会话免问、换类换工具仍问、deny 永远压过记忆）；②用户**拒绝** logo.png 后模型 reasoning「单文件不需要递归」改写为非递归 `Remove-Item -Force` 重发**直过删除**（拒绝报错里的模式描述=把绕法提示给模型）→ 补「回收站枚举签名（`Namespace(0xA)`/`$RECYCLE.BIN`）× 删除动词」双向 2 条定向模式，37→39，单文件删除仍不拦。测试 +2 改 1（类记忆语义翻转），pytest 221 绿；config.example.yaml 注释同步。
- **订正 Concept + Change（审批 Windows 删除缺口补齐）** [[0037]]/[[0038]] — 用户实测事故：`Remove-Item -Recurse -Force` 删回收站项**直过未审批**，根因 = 30 条危险模式 Unix 为中心、Windows 删除命令整体缺口。补 7 条 Windows 模式（remove-item -recurse / rd|rmdir|del|erase /s / clear-recyclebin / format x:），30→37；单文件删除仍不拦（与 Unix `rm file` 对称）。新增 3 个测试（事故原形命令命中/Windows 变体命中/单文件不命中），55 项全绿；config.example.yaml 与 0037 计数同步。
- **新增 Concept + Change（审批系统沉淀）** [[0037_approval-permission-system]] + [[0038_dangerous-operation-approvals]] — **危险操作审批与权限系统 + 落地变更**：0037 沉淀稳定概念（**三值决策管线**借鉴 Claude Code：`evaluate() → allow/ask/deny`，优先级 mode auto > deny 规则 > allow 规则 > 会话记忆 > 危险判定，deny 压过记忆防诱导翻硬拒；四层架构=判定 `_approvals.py` 纯函数/协调 `_approval_shared` 阻塞等待/拦截 dispatch 单门 `tools/__init__.py:248`/通道三模式注入；规则语法 `"tool(glob)"`——terminal/ssh_exec 匹配命令原文、其余 action+关键参数、**无 action 工具判定文本为空只能整名覆盖**、括号贪婪到尾防命令内括号截断；会话记忆=精确匹配+进程内重启即清+按 session_key 隔离+**顶层 chat 写键/子代理共享引用读**的桶对齐；五结局状态机保守失败=无回调立即拒/回调异常拒/中断拒/超时按 timeout_action；`try_resolve_steer` 三入站口折批复整词匹配长文本走 steer；**诚实边界=启发式非安全边界**，变量间接/base64 可绕过，价值在收敛重复确认+不依赖人反应速度的硬拒；页内含**全流程图**（tool_call → dispatch 门 → evaluate 优先级链 → request_approval 状态机 → 三模式通道/回传路径 → always 记忆，开放树形样式同 [[0036]] 装配管线图））。0038 记变更（terminal 死代码分支补全、`_DANGEROUS_PATTERNS` 30 条正则迁入 `_approvals.py`、高危参数表 7 类、桌面审批卡三按钮含「批准，不再询问」、config approvals 五键、pytest 全绿+桌面 build 绿；踩坑：`* mkfs *` glob 要求前导空格而命令开头即 mkfs 不命中→改 `*mkfs*`）。total_pages 37→39。

## 2026-08-19

- **新增 Concept** [[0036_system-prompt-assembly]] — **系统提示词装载与三层 Agent prompt 差异**（用户要求「把 prompt 的装载逻辑沉淀到 wiki，写出 3 层 agent 的差别」）：`core/prompts.py` 声明式 `LAYERS` 表（PromptCtx 12 字段 + Layer 返回节文本或 None，`_tool_guard`/`_passthrough` 工厂，**表序=节序**，`expand_agent_vars` 最后统一展开 `${AGENT_HOME}`，每轮重建）；四层组 18 层注入条件全表（Identity/Discipline/Tool guidance/Runtime context）。**三层三条装载路径**：主 agent = 完整层组（platform + kbs preamble + 专家花名册 + 每轮 API 边界记忆快照 `<memory-context>` 不落库）；专家 agent = **同一条装配代码**只换入参（persona 当 identity_override + memory 命名空间行、platform="agent" 无平台层、无 preamble 改拿 kbs_read_note 读纪律、无 roster 层、指导层**按自身工具面照常裁剪**——与 delegate 的本质区别）；delegate = `chat()` 三处 `system_prompt_override` 短路 → 纯任务卡（YOUR TASK/CONTEXT/WORKSPACE 文本嵌入，无任何指导层，默认名单故意不含 memory_manage）。不变式：条件层 key off ctx.tools 且 available_tools 与 chat loop get_schemas 同过滤器（防指导层宣传不可调工具）、CODING_TOOLS 按写/执行面、`kbs_protocol.md` 改动即时生效 vs .py 需重启。total_pages 36→37。
- **KBS 协议文本瘦身（同日）** — `kbs_protocol.md` 2965→2294 字符（kbs_search 机制三处重复收敛、检索纪律压缩）；`kbs_templates/AGENT.md` **20782→5510 字符（-73%）**：删与 preamble 重复的 9 节（意图映射/自主策略/会话开始/何时查询/检索顺序等，xihe 部署中两者同场必重复烧 token）、领域三节合一、目录→slug 映射表一句化、工作记忆边界并入回写规则；收录 11 步/决议九条账本/领域专项六项逐条保留（49/49 语义不变量脚本核验）；实例 `.biz_kbs/AGENT.md` 已同步。AGENT.md 每次写入前整文 `read_file` 进上下文，~13K→~3.5K token。

## 2026-08-17

- **新增 Concept + Change（三层 agent 名单沉淀）** [[0034_three-layer-agent-roster]] + [[0035_three-layer-roster-unification]] — **主/专家/delegate 三层名单模型 + 统一变更**：0034 沉淀稳定概念（主 agent = config.yaml 顶层 `toolsets`/`skills`，与专家**共用 `resolve_roster`**、无 main 专属逻辑；统一三态语义 不写/`[]`=不加载+告警、`["*"]`=None 全量、名单=白名单且 `mcp`/`mcp-<server>` 永远保留；**`[]` vs `None` 不变式**——truthiness 会把 `[]` 翻成全量，agent.py 必须 `is not None`，曾出反向 bug；`specialists.enabled` 总闸默认关；delegate 运行时三态**独立于父名单**（slim 主不饿死子）+ `subagent_blocked` 12 类全集 + 无技能索引（system_prompt_override 短路）；load_config 白名单**两循环**陷阱；serve `_capabilities` 按主名单收敛；三次设计纠正记录——终态「主 agent 就是根配置，不单独建模」）。0035 记变更（resolve_roster 统一/删 main 专属 resolver；agent.py `is not None` 修复；config.py 白名单两处扩展；`_capabilities` 从裸 registry 收敛到 get_schemas 主名单视图；delegate schema blocked 清单 5→12 修正；桌面 specialists_enabled Toggle + SpecialistsCard 琥珀横幅；破坏性影响：存量不写 toolsets=无工具、不写 specialists.enabled=run_*_agent 消失、需重启）。**同日订正** [[0032]]（页首 ⚠️：toolsets 不再缺省 `[files,memory]`——不写=不加载；skills `["*"]`=全量；specialists.enabled 总闸）。total_pages 34→36。
- **订正 Entity** [[0001_xihe-agent]] — 「配置分层」条目失实（`.env`/`${ENV}` 展开为 2026-08-13 单源化前形态），改为**配置单源**：一个 config.yaml、值全字面、无 env 覆盖；数据根优先级 `~/.xihe-agent` < `AGENT_HOME` < `--config` 的 `agent_home`（定位器例外支持 `${VAR}`）。运行时状态列表同步去掉 `+ .env`。
- **订正 Concept** [[0023_multi-instance-config]] — 页首加 ⚠️ 过时注记：优先级 5 层表/`.env` 浮顶陷阱（7 个 env-aliased key）/示例 `${VAR}` 展开/数据根链「项目 config.yaml.agent_home」层均为单源化前形态；`--config` 机制、peek argv 设计、数据隔离边界仍有效。索引行同步。

## 2026-08-16

- **新增 Concept + Change（专家 agent 知识沉淀）** [[0032_specialist-agents]] + [[0033_specialist-toolset-overhaul]] — **专家 Agent 架构 + 落地变更**：0032 沉淀稳定概念（`agents/<slug>.yaml` 一文件一专家 → 派生 `run_<slug>_agent` 工具 + 花名册路由；完整分层 prompt vs delegate wholesale 覆盖；连接键留空继承主配置；**skills 白名单 None=全量索引/空集=不注入** 的 falsy 翻转陷阱；`mcp-<server>` 按需授权 = get_schemas 双路匹配（静态名单 ∪ registry toolset 字符串）；api_key 永不回显 + PUT 三态（非空写/空串清/缺省保持）；与已废弃角色化 [[0017]] 四个回退理由的逐条对策）。0033 记变更（config.yaml agents section → 每专家一文件 + serve CRUD + 桌面 SpecialistsCard 编辑器含待重启徽标；skills 空集语义 bug 修复；**工具集目录重构**：删组合预设/includes 机制/browser_scripts 死组、core 四拆 files/terminal/dev_tool/http、agent 拆出 skills → 14 平铺组带中文 label；itsm.yaml 迁移；e2e 28 项 + pytest 12 + 桌面 build 全绿）。**同日订正**：[[0013]] 目录表加 ⚠️ 过时注记（裁剪+request_tools 机制仍有效）、[[0002]] toolset 分组条目更新为 14 平铺组+双路匹配、[[0017]] 废弃注记补后继指针、[[0001_xihe-agent]] 补专家 agent 功能点与 agents/*.yaml 运行时状态。total_pages 32→34。
- **新增 Concept** [[0031_workspace-cwd-binding]] — **工作空间 cwd 绑定与入口差异（桌面 vs CLI）**：cwd 绑定在「会话」上（桌面私有 `~/.xihe-desktop/workspaces.json` 的 convWorkspace map）、**随每轮 sendTurn 传给 serve**（`store.ts:471-479` → `serveClient.ts:219` → `serve.py:538-542` `create_agent(cwd=cwd)`）、**不落库**（sessions.db 无 cwd 字段）。入口无关：外层对话列表与工作空间内进入完全一致（都按 convId 查 map）。CLI 完全独立：会话列表只列 platform="cli"（serve 会话不可见）+ 不读 map + cwd=进程启动目录。失效边界 3 条（删空间清绑定 / workdir 不存在被 serve 丢弃 / 外层新建无绑定）。「历史消息 ≠ 运行时 cwd」（消息持久、目录只随轮次）。打通方向备忘：cwd 持久化到 session meta。total_pages 31→32。

## 2026-08-13

- **新增 Concept** [[0030_packaging-distribution-strategy]] — **xihe+desktop 打包发行策略（设计参考，未实现）**：桌面端已迁入 `xihe-agent/desktop/`（electron-vite，仅 `build` 出 `out/`，**未接 electron-builder**）。三平台可行性：**Windows** ✅（Electron + 冻结 `xihe serve` 子进程；4 步=冻结 python(PyInstaller/嵌入式) → electron-builder extraResources → 首跑数据目录+ConfigPanel 引导 → Authenticode） / **macOS** ✅ 但更贵（需 mac 构建机、不能交叉构建；签名+公证气隙做不了→ad-hoc+quarantine/MDM；arm64/x64 + ⚠️paddle mac arm64 wheel 待验证；deep-sign python 树 afterSign；hardened runtime+entitlements；Mac App Store 没戏） / **iOS** ❌ 不能同款（Electron/Python 子进程都不上 iOS）→ 唯一形态=瘦客户端(SwiftUI/RN/Capacitor)连远程 serve/gateway 复用 WS 协议，App Store 政策禁工具调用 agent。核心论点：**打包=决定 Python 大脑跑哪**（本地 bundle vs 远程），serve 协议([[0024]]/[[0029]])是 enabler；Windows 与 macOS 同框架两 target，`resolveXiheBin()`/extraResources/afterSign 设计成可复用。含 electron-builder mac 配置示意 + 跨平台 spawn 路径解析 snippet。建议顺序：先 Windows→macOS→iOS 另立项。total_pages 30→31。

## 2026-08-12

- **候选创建** `external-agent-adapter-protocol`（open）— 调研 codex CLI headless 协议（`codex exec --json` JSONL + thread/item 事件 + 冷 resume + cwd + env 凭据）与已实测 claude stream-json（[[0028]]）做契约对照。**结论：两者同构，通用 external-agent adapter 成立**（NDJSON + 会话id冷resume + cwd + env凭据 + 杀进程中断）。差异：生命周期（claude 长驻多轮 stdin / codex 每轮新进程 = claude 重写前模式可复用）+ 事件 schema（各一份 mapper）+ codex 无 stdin-boot 死锁 + codex 多 `file_change` 事件。最大未验证风险：codex 默认 `wire_api=responses`，内部网关 chat-completions 兼容性存疑。附待决接入模式（MCP vs IPC，倾向 IPC）。codex 未实测走候选，实测通过后提升正式 concept。
- **新增 Concept** [[0029_desktop-dual-engine-architecture]] — **桌面端双引擎架构总览（整体 hub）**：xihe serve（WS）+ claude（STDIO）两引擎并列、统一 `ServeEvent` → renderer 引擎无关归约（`handleEvent` 按 `conv_id` 路由）。含双引擎总览图（两路抵达通道：WS 客户端 vs `claude:event` IPC，都喂同一 reducer）、两路传输对照表（进程模型/并发/中断/会话真理/冷启动/cache/cwd/凭据 12 维）、统一归约层、main 总编排（ServeSupervisor adopt-or-spawn+readiness/liveness/restart × ClaudeRunner 长驻+硬化 × IPC 桥 4 组通道）、ClaudeRunner 生命周期健壮性 5 项（**stdin-boot 死锁真根因**——stream-json 须先收 stdin 首行才 boot，旧 buffer 致双向死等 + interrupt 无条件 teardown + 45s 就绪超时 + 10min 空闲回收 + PID 文件孤儿清扫）、凭据与会话真理归属对比、按角色分流阅读路径。串联 0024/0025/0026/0027/0028，不重复子页细节。total_pages 29→30。
- **订正 Concept** [[0028_desktop-claude-transport-architecture]] — 据 [[0029]] + 硬化后代码就地订正：生命周期表「就绪」行（不再 flush `pendingSend`，`send` 直接 `writeTurn`）+ 补「就绪超时/空闲回收」两行；关键不变式加 **stdin-boot**（stream-json 须先收 stdin 首行才 boot）+ 更新 ServeSupervisor 对比（有一次性就绪超时 + 空闲回收，仍无 liveness 轮询/auto-restart）；中断与销毁节（interrupt/dispose 收敛为 `halt`，无条件 teardown，覆盖 boot 窗口）；已知限制删「无空闲回收」（已落地）+ 指向 0029 硬化节。
- **新增 Concept** [[0028_desktop-claude-transport-architecture]] — **桌面端 claude 接入架构（稳定参考页）**：claude 作为第二种 agent 引擎，**一会话一长驻子进程 + stdin 跨轮喂 NDJSON**；stdout NDJSON → 同形 ServeEvent → 复用 renderer `handleEvent` 归约；冷/热双路径（进程死后 `--resume` 续同会话）实测通过。含整体数据流图、LongLivedSession 生命周期表、NDJSON→ServeEvent 映射、IPC 通道（claude:send/interrupt/dispose + claude:event）、凭据注入、spawn 矩阵、interrupt/dispose 语义分离、持久化、6 条已知限制。是改 claude 传输层 / 排查会话问题的基线。total_pages 28→29。
- **新增 Change** [[0027_desktop-claude-longlived-rewrite]] — **xihe-desktop ClaudeRunner 长驻 stream-json 重写**：`src/main/claude.ts` 从「每轮新进程 + `--resume`」→「一会话(convId)一长驻进程、stdin 跨轮喂 NDJSON」。每轮省 ~5s CLI 启动 + 复用进程内 prompt-cache（热路径 turn2 `cache_read 25856` 命中）。**双路径实测通过**：热路径同进程多轮 stdin 复用 + 冷路径 kill 进程 A → 进程 B `--resume` 答 BLUEFIRE（同 sid 跨进程复活、模型记忆不丢）。用户决策=保留持久化 + `--resume` 作冷启动 → 跨 app 重启续接免费入范围 + 消除「中断丢上下文」权衡。新增 `dispose` 补 `deleteConversation` 空闲进程泄漏缺口。代码完成 + tsc/build 绿。候选 `desktop-claude-longlived-rewrite` promoted。total_pages 27→28。
- **候选决议** `desktop-claude-longlived-rewrite` → **promoted** → [[0027_desktop-claude-longlived-rewrite]]（冷 resume 实测通过，双路径验齐）。

## 2026-08-11

- **新增 Insight** [[0026_desktop-agent-model-built-in-xihe]] — **桌面端 Agent 模型定调**（ADR）：Agent = **类型**，不是几个 xihe 进程、也不是几种人格。**xihe** = 桌面**内置**默认 agent（桌面 **main 进程托管 `xihe serve` 子进程生命周期**——用户永不手敲 `xihe serve`、永不见 "serve" 字样）；**claude** = **可添加**的外部 agent 类型（connector，凭据接入；**当前只留 IA 槽位，不实现**）。**否决**多实例（用户）与多 persona（persona 已在 xihe-agent 回退，[[0017]]/[[0018]]）。**Workspace** = 项目文件夹 / 用户资产（与 agent 正交，一等公民，去掉「高级/隐藏」措辞）。**Manage** 范围**待定**（用户晚点确认；现状 ManagePanel 已只读接 MCP/skills/cron）。下游 **F1**：main 托管 serve + 花名册收敛为单一内置 xihe + 去 "serve" 措辞。total_pages 26→27。
- **订正 Concept** [[0025_desktop-control-plane]] — 据 [[0026]] 就地订正 **Agent 层建模**：摘要 + 三层模型的 Agent 子弹 + 种子数据 + Roadmap（**P1 persona 废弃**、**P2 claude 重构为「可添加 agent 类型」**、新增 **F1** 行、P3 标注 MCP/skills/cron 已只读接入）+ 相关页面。该页控制面定位 / 能力驱动 UI / 三段式 / store / 协议引用仍有效，仅 Agent 层语义以 [[0026]] 为准。

## 2026-08-10

- **新增 Concept** [[0024_desktop-serve-protocol]] — **桌面端↔agent 通信协议(xihe serve 模式)**:第三种运行模式 `xihe serve`(aiohttp HTTP+WS,复刻 gateway 的 SharedContext + 每轮薄 agent,但用 `run_in_executor` 解放事件循环,避开 [[0011]] 的 `thread.join` 阻塞顽疾)。REST(`/health`/`/agents`/`/sessions`/`/convs/{id}/messages`/`/reset`)+ WS `/stream`(send/interrupt ↔ hello/turn_start/text_delta/thought_delta/tool_call/tool_result/complete/error)。**核心机制**=Emitter 桥(agent 工作线程同步回调 → stdlib `queue.Queue` → WS handler 协程排空;为什么不用 asyncio.Queue:工作线程无 running loop)。会话映射 `conv_id`→`SessionSource(platform="serve")`→`agent:main:serve:dm:{conv_id}`→持久 session。能力描述符从 registry 实时推导(check_fn 门控)。并发:`_turn_locks` 同会话串行 + `_active`+`threading.Lock` 中断 + 掉线必中断。CORS(ACAO:*)+ 桌面原生 WS/fetch 硬编码 7788。坑:漏 logging 已修、`args` 是 120 字摘要非全文。total_pages 24→25。
- **新增 Concept** [[0025_desktop-control-plane]] — **xihe desktop 桌面控制面设计**(独立仓 E:\xihe-desktop,v0.0.1 骨架):Electron app **不内嵌引擎**,经 [[0024]] 协议驱动 `xihe serve`。栈=electron-vite + Electron 29 + React 18 + Tailwind + Zustand;三段式 main/preload/renderer(`contextIsolation:true`、**无 ipcMain**、preload 是 stub 死通道)。**设计主梁=三层模型**(Provider→Agent→Session)+ 两种 provider 形态(process 自有 dataRoot / connector provider 持真理)+ **能力驱动 UI**(按 capability flag 分支不 sniff 引擎名,`EngineBadge` 是唯一例外=配色)。Store:种子 3 demo agent,仅 `xihe-ops`(LIVE_SLOT)在 serve 可达时升级、懒加载历史、3s 重连;P0 只渲染 text_delta/complete/error。Roadmap:P0 serve 接入(✅)/P1 persona 层/P2 Claude connector/P3 管理 UI/P4 CodeBuddy。列未完成项(全 demo 数据、ManagePanel 空、无 interrupt UI、无打包、`deliberateClose` 未用)。total_pages 25→26。

## 2026-08-07

- **新增 Concept** [[0023_multi-instance-config]] — **多实例配置**:`xihe --config x.yaml` 启动时选实例(YAML 描述实例:`agent_home`→数据根隔离 + 最高优先级配置覆盖)。**设计方案**=peek argv(`config.py` import 时扫 `sys.argv` 算对 `AGENT_HOME`,0 下游重构;否决函数化的 ~30 touch-point,理由:与读 `AGENT_HOME` env 同质)。**优先级**:默认 < 用户 yaml < 项目 yaml < `--config` yaml < `.env`(env 浮顶)。**陷阱**:7 个 env-aliased key(`model`/`api_key`/`base_url`/`platform`/...)会被 `.env` 抢占,实例要不同须放 sibling `.env`(`configs/.env`)而非 yaml(实测 yaml 的 `model` 被仓库根 `.env` 的 `MODEL` 压)。`.env` 加载序:sibling > 仓库根 > `AGENT_HOME`。数据隔离:sessions.db/log/browser/cron 随 `agent_home`。落地 4 文件,12 测试全绿 + smoke。total_pages 23→24。
- **新增 Concept** [[0022_testing-strategy]] — xihe 测试策略分层模型:L0 纯函数 / L1 工具 handler(mock IO) / L2 agent 循环+注入假模型(`FakeChatClient`) / L3 真 LLM+judge / L4 平台集成。关键技巧:让 `XiheAgent.__init__` 收 `client=None`(向后兼容),测试注入按剧本返回响应的假 client → 循环变确定性。同日落地 `tests/` 骨架(pytest + conftest/fakes + 4 个 test_*.py、12 测试全绿,含直接保护 `_last_exit_reason` 的撞墙测试)。隔离约定:monkeypatch `core.session._DB_PATH` 到 tmp + `is_subagent`/`system_prompt_override` 跳副作用。L3/L4 记为后续。total_pages 22→23。

## 2026-07-31

- **新增 Change** [[0021_cli-hybrid-tui]] — **功能点**:CLI 交互重写为 hybrid 模式(空闲 prompt-toolkit 历史/补全 + 处理中 msvcrt 非阻塞 steer)。同时含:cwd 注入(系统提示词 + 缓存失效)、fresh-by-default 会话、来晚 steer 自动续跑(msgs 队列 + ❯ 回显)、CODING_GUIDANCE 7 点增强(先读再改/搜索优先/最小改动+验证/安全)、日志 file_level 解耦、工具开始时 ⏺ 打印。踩坑全记(prompt_toolkit patch_stdout 乱码、Textual Windows 输入不工作、Rich Console 清行 → 最终 hybrid 方案)。total_pages 21→22。

## 2026-07-30

- **新增 Change** [[0019_kbs-feature]] — **功能点**:KBS 子系统(可插拔业务知识库协议)。总开关 `kbs.enabled` 同时门控前导注入与工具可见性(`check_fn`)、关掉零足迹;新增 `kbs_init`(模板盖章建库)/`kbs_status`(健康摘要)/`kbs_search`(index-first 检索,未命中才 grep 兜底)三工具,文件读写复用 core 工具不重造;精简版协议前导 `core/kbs_protocol.md` 固定打包;空白模板 `core/kbs_templates/`。**背景**:与 `.biz_kbs`(业务知识库)区分——本功能是"让 xihe 能用 .biz_kbs",属 xihe 代码功能,记在本库 changes(不是 concept)。补 [[0001_xihe-agent]] 总览功能点。total_pages 19→20。

- **新增 Change** [[0020_session-management-commands]] — **功能点**:会话管理命令。`/sessions`(gateway 按当前 user 过滤、默认隐藏 cron/delegate 内部转录)、`/history [N]`(默认 20、解决 gateway 截断)、`/resume [<n|name>]`(会话内切换,CLI only)、`xihe chat -r/--resume`(启动选号);后端 `SessionDB.list_sessions(limit,platform,user_id,include_internal)`。机制:resume = 复用 session_key → load_messages(非新机制);会话内切换靠 `cmd_ctx` 持有可变 `cli_source`。与 Claude 对比(确定性 key vs UUID、SQLite vs JSONL、缓存 vs 重生成提示词)。total_pages 20→21。

## 2026-07-22

- **新增 Concept** [[0018_tool-skill-workflow-role-layering]] — 工具/技能/角色三层分层架构：tool（原子）→ skill（积木，编排型=workflow）→ 角色（执行体）。workflow 是 skill 的编排用法（非单独机制，曾考虑自动注入引用 skill 但调研发现 skill 已够，放弃）。关系图（view/委派/绑定）+ 角色配置速查 + MCP 差集加载。澄清 skill vs 角色（流程 vs 执行体，正交）。**订正** [[0017_role-based-subagents]]（子 agent tool 边界改 `subagent_blocked` 标签 + `is_subagent` 构造属性，`skill_view`/`skills_list`/`todo` 放出）+ 补 [[0018]]（tool 标签机制 + is_subagent 职责分离）+ 补 [[0017]] 主/角色完整差别对照表 + 三设计取舍（为什么不加载上下文/skill 索引、能否再委派）。**回退角色化**（方案设计问题：主 agent 路由负担/隔离副作用/绑定注入 token/边界模糊）——删 `roles.py` + `agents/` + delegate role + MCP 分组 + skill 索引精简，ssh 回 DEFAULT_TOOLSETS，回归单 agent + request_tools + skill_view + ad-hoc delegate。[[0017]]/[[0018]] 标注废弃。

## 2026-07-21

- **新增 Concept** [[0017_role-based-subagents]] — 角色化子 agent：预定义角色（专属 prompt+toolset+绑定 skill+MCP），主 agent `delegate_task(role=...)` 委派。照搬 skill 加载机制（`core/roles.py`）。主子**能力统一（role 驱动）、定位区分**（上下文隔离/无对话通道/递归深度/无 skill_view）。瘦身落点：skill 索引精简（角色绑定 skill 移出主索引）+ MCP 按角色分组（被绑定 mcp 不挂主 agent，request_tools 兜底）。`role_mode` 放宽父交集让角色拥有主 agent 没挂的 MCP。修正 skill 的 user/bundled 优先级 bug。对 cron（全工具不受分组影响）/skill_manage（删绑定 skill 脏引用 graceful 跳过）影响已分析。首个角色 `web-ops`（行内网站操作，绑 cmdb-query-variable + intranet-sites）。

## 2026-07-20


## 2026-07-17

- **新增 Concept** [[0016_interrupt-stop-steer]] — gateway 任务运行时的控制通道设计：停止走带外通道(绕过消息队列) + per-agent contextvar 中断(按会话隔离) + 两套可中断 primitive(`interruptible_iter` 循环 / 子进程注册表+`run_interruptible`) + steer(中途注入/自然结束转新轮/停止丢弃) + 收尾 `✅任务已停止` + 7 个演进坑(队列阻塞/全局中断串/工具不轮询卡死/60s并行超时/流式40008/重排-on-stop/停止词不全)。

## 2026-07-16

- **新增 Insight** [[0009_agent-security-master-identity]] — agent 安全：prompt 层 vs 代码层防护分析、CaMeL/Lethal Trifecta/Claude Code 权限系统调研、商用方案对比、xihe 推荐方案（敏感路径黑名单+输出脱敏+chat_id 绑定+内容来源标注），含攻击路径和实施优先级。

## 2026-07-13

- **新增 Concept** [[0014_project-context-loading]] — 项目上下文加载规则：全部合并（不是 first-match-wins）+ .xihe.md/AGENTS.md 始终加载 + CLAUDE.md/.cursorrules 可配置 + token 影响分析。

## 2026-07-10

- **新增 Concept** [[0013_toolset-scope-and-dynamic-expansion]] — toolset 分层（always-on core/memory/comm/agent/mcp vs on-demand web/media/scheduler）+ 默认裁剪省 ~4-6k token/轮 + request_tools 元工具按需展开 + 为什么不用向量检索/LLM 分类。

## 2026-07-09

- **新增 Change** [[0012_new-tools-and-gateway-enhancements]] — 新增 3 个开发者工具（http/maven_dep/node_version）+ web 工具 check_fn 门控（无 API key 时自动隐藏，减少 prompt 开销）+ gateway/file_tools/mcp_tool/cronjob_tools/wecom 适配器增强。

## 2026-07-06

- **新增 Concept** [[0011_gateway-architecture]] — 沉淀 gateway 架构：SharedContext 拥有重对象（db/aux/compressor）+ 每消息 `create_agent()` 建薄 agent + SQLite 续对话 + 模块全局共享状态（浏览器/MCP/cron/adapter）+ 阻塞 join 的并发模型（同/跨 session 被动串行、interrupt 难触发、并发化需加 per-session guard）。

## 2026-07-03

- **变更记录** [[0010_cdp-default-and-cron-job-forms]]（已验证落地）：
  - 浏览器默认改 CDP 托管真实 Chrome（`cdp-profile` + 9222），persistent 降为兜底；新增 `browser_logout`；顺带把"注册未列"的 `browser_login` 补进 toolset。解决内网 SSO（门户/passport）Secure cookie 在 http 回调被丢、登不进的问题。
  - cron job 三形态（纯 prompt / `no_agent` 纯脚本 0 token / 脚本喂 prompt）+ wake gate（`{"wakeAgent": false}` 静默跳过）+ `context_from` 链式。
  - 提示词：`scratch/<任务名>/` 目录约定、`CRON_GUIDANCE`、撞登录墙主动开登录页。
- **新增 Concept** [[0009_cron-jobs]] — cron 调度基线（无状态会话）+ 三形态 + 脚本约定。
- **订正 Concept** [[0003_browser-tools]] — 启动策略从「persistent 为主」订正为「CDP 默认 + persistent 兜底」，补充 `browser_logout`。原文「三层认证」框架已过时。
- **注意**：浏览器/cron/提示词改动均需重启 gateway 生效（system prompt 与工具 schema 按进程缓存）。

## 2026-07-01

- **收录** `.docs/` 7 篇设计文档入 wiki:
  - 原文快照入 `raw/sources/`（不可变）
  - 新增 Concept: [[0002_tool-registry-and-dispatch]] / [[0003_browser-tools]] / [[0004_context-compression]] / [[0005_mcp-dynamic-registration]] / [[0006_session-design]] / [[0007_skills-system]]
  - 原 `.docs/` 已删除（移入 wiki）
- **初始化** 项目 wiki 骨架（入口页 active / recent / index / log、meta、schemas、raw/sources）。
- **新增** Entity: [[0001_xihe-agent]] — 基于 `CLAUDE.md` 的项目总览种子页面。
