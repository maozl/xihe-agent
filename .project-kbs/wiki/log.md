# 维护日志

> 按时间顺序记录的收录、回写、查询、整理操作。

## 2026-08-26

- **落地**: codex `base_url` 透传 + `extra_args` 逃生口（用户要求「做吧，把 base_url 透传实现进 CodexDriver + 增加一个透传特殊配置的配置项」）— `external_agents.codex.base_url` 设置时 driver 发五连 `-c` 内联定义 provider `xihe`（model_provider/name/base_url/env_key=CODEX_API_KEY/wire_api），覆盖该轮 config.toml 的 provider 选择；**opt-in 不回退主 base_url**（否则每个实例都悄悄改写 config.toml 选好的 provider）；`wire_api` 随行默认 `responses`（非法回落）、值原样透传不剥 `/v1`（codex 自拼 /responses 或 /chat/completions）。`extra_args`（list）原样追加、位于各引擎 resume 旗标之前（codex：resume/"-" 前；claude：--resume 前——用户中途追加要求 claude 同样支持；`-c` 键值对/`--ephemeral`/`--settings` 等）；`TurnSpec` 加 `extra_args` 字段。代码：`external_agent.py` 双 `_spawn`/`TurnSpec`、`external_agent_tool.py` `_resolve_llm_creds` codex 分支/`_norm_extra_args`/spec 构造/schema description、`config.example.yaml` 两引擎块。测试 +4（内联 provider 五连 -c 与 wire_api 回落/extra_args 位于 resume 前/creds opt-in 不回退/claude --resume 前位次）；pytest 348 绿 + 真实 E2E 两发（内联 provider 覆盖 config.toml 的 zp → INLINE-PROVIDER-OK；--ephemeral → EXTRA-ARGS-OK）。[[0040]] argv 契约/对照表/协议要点/落地节同步。**注意：.py 改动需重启 serve/gateway 生效。**
- **订正**: [[0040]] 凭据表述 + 代码注释（用户带来 codex `-c` 覆盖说明并要求验证）— 本机实测通过：纯 `-c model_provider/model_providers.<id>.base_url|env_key|wire_api` 定义新 provider 打内部网关全链路可用（0.146.1）；「base_url 不吃 env」仍成立，但「只认 config.toml」改为「实现选择非能力限制」。改口径五处：0040（对照表凭据行 + 协议要点凭据 bullet）、`external_agent.py` spawn 注释、`external_agent_tool.py` docstring + schema description、`config.example.yaml` codex 注释。同步 recent.md；未实现 `-c` 透传（如需 `external_agents.codex.base_url` 再议）。
- **候选决议**: `external-agent-adapter-protocol` open → **promoted**（用户要求「提升为正式 concept」）→ 新增概念页 [[0040_external-agent-adapter-protocol]](type=concept)，与 [[0028]] 组「外部 agent 接入协议」双页（0040 = xihe 内核侧协议对照与驱动架构，0028 = 桌面端 claude 传输层）。内容取自候选已验证部分转正为稳定参考：双引擎驱动架构（共享机制层 + WARM/ONE-SHOT 两策略）、协议对照表 13 维、codex headless 协议要点与事件映射、两个平台硬坑、IPC 决策（含理由）、开放点（能力共享边界 / app-server 长驻）随页转入。候选闭环（status promoted + resolved_at/resolution_target + 决议节）；同步 candidates/index.md（开放候选 2→1 + 已决议行）、wiki/index.md（0040 条目）、recent.md、active.md（探索条目改指 0040 + 知识库现状 39→40）；total_pages 39→40。
- **候选更新**: `external-agent-adapter-protocol`（用户要求「更新候选页」）— codex 本机实测（0.146.1 + 内部 litellm 网关）通过：Responses API 直连可用（原最大风险消除，无需 wire_api="chat"）；新增两个硬坑记录（`--disable multi_agent` 必带——namespace 工具 500 被包成 "high demand" 误导；Windows sandbox elevated 1385 → unelevated）；stdin EOF-boot 门、resume argv 形态、sandbox 三值枚举、base_url 只认 config.toml 等协议校订；双引擎 driver 落地内核（共享机制层 + WARM/ONE-SHOT 两策略，(engine, session_key) 双键 resume 表）+ 测试 11 项 + 全量 344 绿 + E2E 两轮。候选保持 open（待提升为正式 concept 与 [[0028]] 组双页）；同步 candidates/index.md、active.md 过时注记、recent.md；last_writeback 更新。

## 2026-08-25

- **回写**: 审批系统三步扩展（用户四步递进需求：write 工具纳入拦截→要求通用性；记忆落盘；`memory_days` 非法回落默认 30 无 0 开关；cron 按任务名读记忆 + 工作空间按目录 + cron 支持审批——用户否决「手动触发改同步执行」，定调审批卡投递闭环）→ 新增变更页 [[0039_ask-rules-approval-dimensions]](type=change) + 订正 [[0037_approval-permission-system]]（摘要补 ask/落盘/维度/后台审批卡；管线块加 ask 规则层；会话记忆节改「审批记忆」=落盘+TTL+三维度；新节「后台审批（cron 定时任务）」；流程图重编号加 ask 步骤与 cron 通道；尾注桶键来源 approval_key；相关页补 0039）。代码点：`_approvals.py` ask 规则层/`_generic_summary`/落盘（懒水合+原子写+滑动续期+mtime 清扫+fail-open）/`_pending_external` 路由表；`agent.py` `chat(approval_key=)`；`serve.py` `_ws_approval_key`；`bot.py` `_handle_steer` 折后台卡；`cronjob_tools.py` `_make_approval_callbacks`/`_interrupt_job_run`/`_active_agents`。**同日全路径完备性检查**发现并修复路由表登记键错误（deliver 平台名 → 实际适配器名）。测试：test_approvals.py +17、新 test_cronjob_approval.py 7 测、conftest `_pending_external` 隔离；pytest 全绿。同步 config.example.yaml approvals 段 + active.md 过时注记订正（「cron 无人值守即拒」→ 已升级审批卡闭环）。total_pages 38→39，last_writeback 更新。

## 2026-08-20
- **回写**: LLM 语义判定层落地（用户提问「正则枚举没法覆盖所有的吧，加一个 llm 判定比较好」→ AskUserQuestion 裁决「现在落地」+「默认开」）— `_approvals.py` 新增 `_SUSPECT_RE` 漏斗 / `_llm_judge_command`（aux.call_llm approval_judge 任务，严格 JSON+固定类别枚举）/ `_maybe_llm_judge` / `_judge_tls` 类键传递；evaluate 加 aux 参数尾部级联；dispatch 传 `agent.aux`（三模式全有，零接线）；auxiliary_client `_DEFAULT_TIMEOUTS` + approval_judge:10；config approvals 加 llm_judge。测试 +8；[[0037]]（摘要/管线块/新节「LLM 语义判定层」/流程图第 6 步/诚实边界两次补丁对照）、[[0038]]（判定层/六键/验证）、config.example.yaml、recent.md 同步；pytest 229 绿。
- **回写**: 会话记忆类级化 + 拒绝后改写绕过补网（用户实测反馈「批准不再询问后删下一个又问」+ 日志复查发现拒绝后改写绕过）— 证据：agent.log 14:54/14:55/14:55 三次删除审批（always 两次+deny 一次）+ sessions.db 消息 528359（模型自述「换用非递归方式」）。修正一：`_approvals.py` 记忆升级类级（`_danger_detail` 类键提取 / `remember_rule(+config)` 双键 / evaluate 两类键命中 / dispatch 传 config）；修正二：+2 条回收站定向模式（枚举签名×删除动词双向），37→39。测试改 1 增 2；[[0037]]（摘要+决策管线+会话记忆节+流程图+诚实边界改写实证）、[[0038]]（判定层+边界节）、config.example.yaml、recent.md 同步；pytest 221 绿。
- **回写**: 审批 Windows 删除缺口补齐（用户实测反馈「删回收站文件没让我审批」）— 查 agent.log 定位 14:44:26 的 `powershell Remove-Item -LiteralPath 'E:\$RECYCLE.BIN\...' -Recurse -Force` 直过；根因 = 危险模式表 Unix 为中心（Windows 仅 taskkill/stop-process 杀浏览器两条）。补 7 条 Windows 删除模式（remove-item -recurse / rd|rmdir|del|erase /s / clear-recyclebin / format x:）至 37 条；test_dangerous_command.py +3（事故原形/变体/单文件负例）；config.example.yaml、[[0037]]（摘要+流程图+诚实边界）、[[0038]]（边界节事故记录）、recent.md 同步。同日清理 terminal.py 死代码（不可达外层 TimeoutExpired / no-op _check_terminal / 未用 pattern_key），52+3 测试绿。
- **回写**: 审批系统沉淀（用户明确要求「完成后把审批相关的沉淀到wiki」）→ 新增概念页 [[0037_approval-permission-system]](type=concept) + 变更页 [[0038_dangerous-operation-approvals]](type=change)。沉淀前代码核实：dispatch 门实际代码（tools/__init__.py:248-266）、request/resolve 五结局（agent.py:369-454）、子代理共享点（delegate_tool.py:150 / specialist_agent_tool.py:77）、三模式接线（serve.py:870/935/1031/1039、server.py:221/356、chat.py:390/451）、`_approvals.py` 全文。0037 沉淀稳定概念（三值决策管线 allow/ask/deny 及优先级——deny 压过 allow 与会话记忆；四层架构判定/协调/拦截/通道；规则语法 "tool(glob)" 与 rule_text 判定文本——terminal/ssh_exec 取命令原文、其余 action+关键参数、无 action 工具为空串只能整名或 `*` 覆盖；括号贪婪到尾；会话记忆精确匹配+进程内+session_key 隔离+顶层写键子代理共享引用读的桶对齐设计；五结局状态机保守失败——无回调立即拒/回调异常拒/中断拒/超时按 timeout_action；try_resolve_steer 整词匹配长文本走 steer；诚实边界=启发式非安全边界变量间接可绕+与 Claude Code 对照）。0038 记变更（terminal.py 死分支删除、_DANGEROUS_PATTERNS 30 条迁移、高危表 7 类、桌面审批卡三按钮、config approvals 五键、pytest 全绿+桌面 build 绿、`* mkfs *`→`*mkfs*` glob 踩坑）。total_pages 37→39，last_writeback 更新。

## 2026-08-19

- **回写**: prompt 装载逻辑沉淀（用户明确要求「把prompt的装载逻辑沉淀到wiki，需要写出3层agent的差别」）→ 新增概念页 [[0036_system-prompt-assembly]](type=concept)。沉淀前代码核实：delegate 走 `system_prompt_override`（delegate_tool.py `_build_child_agent`）在 chat() 三处短路（首轮取用 + 压缩后两处重建跳过）；专家与主 agent **同一条** `_build_system_prompt` → `build_system_prompt`，专家 SessionSource platform="agent" 不命中 PLATFORM_PROMPTS；kbs preamble/roster 层在 `_build_system_prompt` 内 is_subagent gate；记忆快照 `chat()` 内 `if not self.is_subagent` 于 API 边界注入。页面含装配管线图、18 层条件表、三层差异表（主/专家/delegate 逐维度）、7 条不变式（含 first-build dump 排查入口、.md 即时生效 vs .py 重启边界）。total_pages 36→37。
- **回写**: KBS 协议文本瘦身（同日，用户逐项裁决「修改」「动手，需要确保核心语义不变」）— `kbs_protocol.md` 2965→2294；`kbs_templates/AGENT.md` 20782→5510（删与 preamble 重复 9 节 + 领域三节合一 + 映射表一句化 + 工作记忆边界并入回写；收录 11 步/决议账本/领域专项检查逐条保留，49/49 不变量脚本核验 + pytest 131 绿；实例 `~/.xihe-agent/.biz_kbs/AGENT.md` 已同步覆盖）。

## 2026-08-17

- **回写**: 三层 agent 名单沉淀（用户明确要求「把这3层agent的设计逻辑沉淀到wiki」）→ 新增概念页 [[0034_three-layer-agent-roster]](type=concept) + 变更页 [[0035_three-layer-roster-unification]](type=change)。0034 沉淀稳定概念：主 agent = config.yaml 顶层 `toolsets`/`skills`（与专家**共用 `resolve_roster`**，无 main 专属逻辑——三次用户纠正的终态「主 agent 就是根配置」）；统一三态（不写/`[]`=不加载+告警、`["*"]`=None=全量、名单=白名单，`mcp`/`mcp-<server>` 永远保留待服务器注册）；**`[]` vs `None` 不变式**（truthiness 把 `[]` 翻成全量，agent.py 必须 `is not None`，get_schemas 契约 None=全部/set()=空）；`specialists.enabled` 总闸默认关（关=run_*_agent 不注册+花名册层按可调用工具过滤自动消失，/specialists 回 specialists_enabled 区分「配置关」vs「待重启」）；delegate 运行时三态**独立于父名单**（slim 主不饿死子，安全靠 subagent_blocked 非名单继承）+ blocked 12 类全集 + MAX_DEPTH=2/硬帽 60 + 无技能索引（system_prompt_override 短路 _build_system_prompt）；load_config 白名单**两循环**（拷贝+setdefault）新键静默消失陷阱；serve `_capabilities` 按主名单收敛（get_schemas 视图，不虚报 browser/mcp）。0035 记变更与破坏性影响（存量不写 toolsets=无工具、不写 specialists.enabled=run_*_agent 消失、旧专家 yaml 缺省 [files,memory] 失效、需重启）+ agent.py is-not-None 反向 bug 修复 + config.py 两处白名单扩展 + delegate schema blocked 清单 5→12 + 桌面 specialists_enabled Toggle/琥珀横幅 + `_capabilities` 三态实测（slim 无 browser/vision、None 有、[] 基础六项）。**同日订正**: [[0032]]（页首 ⚠️ 订正注记 + 字段表：toolsets 不再缺省 [files,memory]；skills ["*"]=全量；specialists.enabled 总闸；related_pages 补 0034/0035）。验证：pytest 41 + 三层 walkthrough（L1 slim 13 工具/L2 专家 5 工具无泄漏/L3 默认 49+star→None）+ 桌面 build 绿。total_pages 34→36。
- **订正**: [[0001_xihe-agent]] 配置描述——「配置分层（项目>用户>`.env`，`${ENV}` 展开）」改为「配置单源」（2026-08-13 起：一个 config.yaml、值全字面、无 `.env`/env 覆盖/`${VAR}` 展开；数据根 `~/.xihe-agent`<`AGENT_HOME`<`--config` 的 `agent_home`），运行时状态列表去掉 `+ .env`。核对 `src/core/config.py` docstring 与实现后落笔。
- **订正**: [[0023_multi-instance-config]] 页首加 ⚠️ 过时注记——「优先级 5 层表」「`.env` 浮顶陷阱（7 个 env-aliased key）」两节、示例 `${WECOM_SECRET}`、数据根链的「项目 config.yaml.agent_home」层均为单源化前形态；仍有效部分（`--config` 机制/peek argv 决策/数据隔离/`XIHE_CONFIG_FILE`）显式列出。索引行同步去 `.env` 陷阱描述。

## 2026-08-16

- **回写**: 专家 agent 知识沉淀（用户明确要求「把专家agent相关知识沉淀到wiki」）→ 新增概念页 [[0032_specialist-agents]](type=concept) + 变更页 [[0033_specialist-toolset-overhaul]](type=change)。0032 沉淀稳定架构：每专家一文件 `agents/<slug>.yaml`（slug 正则兼路径穿越防线）→ `load_all_tools()` 派生 `run_<slug>_agent` 工具（agent toolset + subagent_blocked）+ 花名册路由；完整分层 prompt（persona 只换身份层）vs delegate wholesale 覆盖；连接覆盖 `config_overrides()` 非空键 dispatch 时 overlay（父 config 不就地改）；**skills_allowed 三态**（None=主 agent 全量索引 / set()=不注入任何技能 / 非空=白名单）+ falsy 翻转陷阱（e2e 断言守护）；`mcp-<server>` 按需授权零调度侧改动的原理（get_schemas 双路匹配：TOOLSETS 名单 ∪ entry.toolset 字符串，故 register 的 toolset 必须镜像组名）；api_key 安全（GET 只回 api_key_set；PUT 三态=非空写/空串清/缺省保持，实现顺序敏感）。0033 记变更：存储迁移 + serve CRUD（slug guard/原子写/幂等删/未校验编辑器视图）+ 桌面 SpecialistsCard（chips + 旧名虚线保留 + 待重启徽标读活 registry）+ skills 空集 bug 修复 + 工具集 14 平铺组重构（用户五步逐轮裁决：删组合→删 browser_scripts→agent 拆 skills→删 includes→core 四拆 dev_tool 命名）+ 破坏性影响（旧组名 YAML 告警剔除回退默认、9 个工具模块 register 重定向、存量 itsm.yaml 迁移）。**同日订正**：[[0013]] 加 ⚠️ 目录过时注记（裁剪+request_tools 机制仍有效）、[[0002]] toolset 条目改 14 平铺组+双路匹配、[[0017]] 废弃注记补后继指针（0032 对四个回退理由的对策）、[[0001_xihe-agent]] 补功能点+运行时状态+相关页。验证全绿（e2e 28/pytest 12/desktop build）。total_pages 32→34。
- **回写**: 工作空间 cwd 绑定与入口差异 → 新增概念页 [[0031_workspace-cwd-binding]](type=concept)。背景：用户发现工作空间下的对话在外层列表也可见，问「从外层进去 xihe 还知道工作目录吗」「CLI 模式是不是就丢了」。经代码核实沉淀：绑定三层链路（UI `App.tsx:55-61` 派生+文件树 / 发送 `store.ts:471-479` 每轮解析 workdir / serve `serve.py:538-542` is_dir 校验+`create_agent(cwd=cwd)`）+ 入口无关原理（按 convId 非导航路径）+ 失效边界（`store.ts:720-724` 删空间清绑定、`serve.py:539-541` workdir 不存在丢弃、外层新建无绑定）+ CLI 独立性（`chat.py:305/335` platform="cli" 过滤会话不可见 + map 桌面私有 + [[0021]] 进程 cwd 语义）+ 「历史消息 ≠ 运行时 cwd」生命周期区分 + 打通方向（session meta 持久化，未实现仅备忘）。total_pages 31→32。
- **仅建议未写入**（用户明确「不需要」）：2026-08-14 的运行时调优（index.ts Chromium 噪音抑制、serve.ts 就绪超时 15→30s、execute_code.py UTF-8、title_generator.py max_tokens 512/timeout 30）不进 wiki，留代码注释。

## 2026-08-13

- **回写**: xihe+desktop 打包发行策略 → 新增概念页 [[0030_packaging-distribution-strategy]](type=concept，设计参考/未实现)。背景：用户问「把 xihe+desktop 整体打包成 Windows/iOS 可运行软件怎么做」，经 Windows/macOS/iOS 三轮讨论后用户要求「整理成文档放到 wiki，后面再做」。沉淀：三平台可行性表（Windows ✅ / macOS ✅-更贵 / iOS ❌ 不能同款）+ Windows 4 步（冻结 `xihe serve`(PyInstaller/嵌入式 python，paddle 做可选模块) → electron-builder extraResources → 首跑数据目录+ConfigPanel 引导写 config(不带凭据出货，走 [[0023]] --config 机制) → Authenticode）+ macOS 6 增量（必须 mac 构建机不能交叉构建 / 签名+公证气隙做不了→ad-hoc+quarantine 或 MDM / arm64-x64-universal + ⚠️paddle mac arm64 wheel 待验证 / deep signing afterSign 签 python 树 / hardened runtime+entitlements / 数据根 ~/Library + Mac App Store 没戏）+ iOS 真相（Electron/Python 子进程都不上 iOS → 瘦客户端(SwiftUI/RN/Capacitor)连远程 serve/gateway 复用 WS 协议；App Store 政策禁工具调用 agent）+ 共同主干（**打包=决定 Python 大脑跑哪**：本地 bundle vs 远程；serve 协议 [[0024]]/[[0029]] 是 enabler——非进程内嵌才让两种位置都成立；Windows 与 macOS 同框架两 target，resolveXiheBin/extraResources/afterSign 设计成可复用）+ 风险（paddle 跨平台/气隙构建资源/LLM 可达性/签名证书/首跑耦合 --config 改造）+ 落地顺序（先 Windows→macOS→iOS 另立项）。含 electron-builder mac 配置示意 + 跨平台 spawn 路径解析 snippet。事实核对：desktop 已迁入 `xihe-agent/desktop/`（package.json 实测 electron-vite + 仅 build 出 out/、未接 electron-builder）。total_pages 30→31。

## 2026-08-12

- **候选创建**: `external-agent-adapter-protocol` open — 调研 codex CLI headless 协议（`codex exec --json` JSONL + thread/item 事件 + 冷 resume + cwd + env 凭据）与已实测的 claude stream-json（[[0028]]）做契约级对照。**结论：两者同构，通用 external-agent adapter 成立**（NDJSON + 会话id冷resume + cwd + env凭据 + 杀进程中断）。关键差异：生命周期（claude 长驻多轮 stdin / codex 每轮新进程 = claude 重写前模式，代码可复用）+ 事件 schema（各一份 mapper）+ codex 无 stdin-boot 死锁 + codex 多 `file_change` 事件。记最大未验证风险：codex 默认 `wire_api=responses`，内部网关 chat-completions 兼容，能否跑通存疑。附待决接入模式（MCP vs IPC，倾向 IPC，见同期对话）。codex 未实测故走候选。
- **候选更新**: `external-agent-adapter-protocol` 接入模式子决策 → **定 IPC**（不走 MCP）。理由：agent 长时/有状态/流式/可中断特性与 MCP tool-call 请求-响应语义错配（IPC 原生 NDJSON 直连 1:1 契合）+ 产品语义是「借脑」会话级 delegate + ClaudeRunner 已写可复用 + 进程开销来自 agent 本身换 MCP 省不掉（控进程靠生命周期：懒启动 / [[0029]] 空闲回收 / codex one-shot，与协议正交）。混合暴露：传输 IPC + 触发层可叠 MCP tool。codex 实测未完，候选仍 open。
- **回写**: 桌面端整体架构总览 → 新增概念页 [[0029_desktop-dual-engine-architecture]](type=concept，hub/索引页)。背景：0024/0025/0026/0027/0028 各覆盖一引擎或一决策，无一页以「双引擎同时存在」为主语。新页填空白：双引擎总览图（两路抵达通道 WS/IPC 汇入同一 `handleEvent`）+ 两路传输对照表（12 维）+ 统一归约层（`ServeEvent` 公约数）+ main 总编排（ServeSupervisor/ClaudeRunner/IPC 桥）+ ClaudeRunner 生命周期健壮性 5 项（**stdin-boot 死锁真根因修复**——`--input-format stream-json` 须先收 stdin 首行才 boot，旧 send 把首轮 buffer 致双向死等 + interrupt 无条件 teardown + 45s 就绪超时 + 10min 空闲回收 + PID 文件孤儿清扫）+ 凭据与真理归属 + 按角色分流阅读路径。不重复子页细节。同源订正 [[0028]] 与硬化后代码的矛盾（就绪行/不变式/中断销毁收敛/删失真限制「无空闲回收」）。total_pages 29→30。
- **回写（已验证）**: xihe-desktop ClaudeRunner 长驻 stream-json 重写 → 新增变更页 [[0027_desktop-claude-longlived-rewrite]] + 新增稳定架构概念页 [[0028_desktop-claude-transport-architecture]] + 候选 `desktop-claude-longlived-rewrite` promoted。冷 resume 实测通过（kill 进程 A → 进程 B `--resume` 答 BLUEFIRE），双路径验齐。0028 沉淀完整架构（数据流/生命周期/IPC/协议映射/冷热路径/限制）。
- **候选决议**: `desktop-claude-longlived-rewrite` open → promoted（目标 [[0027]]）。
- **元数据**: total_pages 27→29，last_writeback/lint 更新。

## 2026-08-11

- **回写**: 桌面端 Agent 模型定调 → 新增 insight [[0026_desktop-agent-model-built-in-xihe]](type=insight, ADR)。含:Agent = **类型**（xihe 桌面**内置**——main 进程托管 `xihe serve` 子进程生命周期；claude **可添加** connector 占位，本阶段不实现）；**否决**多实例（用户当场否定）与多 persona（[[0017]]/[[0018]] 已回退）；Workspace = 项目文件夹/用户资产（与 agent 正交，一等公民）；Manage 范围**待定**（用户晚点确认）。下游 **F1** 三件套（main 托管 serve + 花名册收敛为单一内置 xihe + 去 "serve" 措辞）。备选方案对比表（类型 vs 多实例 vs persona）+ 风险（子进程生命周期/claude 占位误导）+ 缓解。
- **订正**: [[0025_desktop-control-plane]] 据 [[0026]] 就地订正 Agent 层建模——摘要加 ⚠️ callout、三层模型 Agent 子弹、种子数据、Roadmap（P1 persona **废弃**、P2 claude 重构为「可添加 agent 类型」、新增 **F1** 行、P3 标注 MCP/skills/cron 已只读接入）、设计权衡节加历史快照提示、相关页面加 [[0026]]。该页控制面/能力驱动/三段式/store/协议引用仍有效。total_pages 26→27。

## 2026-08-10

- **回写**: 桌面端↔agent 通信协议 + 桌面控制面设计 → 新增两个共生概念页 [[0024_desktop-serve-protocol]]、[[0025_desktop-control-plane]](大量双向交叉链接)。[[0024]] 记 `xihe serve` 第三运行模式(aiohttp HTTP+WS,复刻 gateway SharedContext + 每轮薄 agent,改用 `run_in_executor` 解放事件循环避开 [[0011]] `thread.join` 阻塞) + REST/WS 契约 + Emitter 跨线程桥(stdlib `queue.Queue`,工作线程无 loop 故不能用 asyncio.Queue) + 会话映射(platform="serve") + 能力描述符(registry 推导) + 中断/并发/掉线必中断 + CORS + 漏 logging 已修。[[0025]] 记桌面控制面(独立仓 E:\xihe-desktop,v0.0.1 骨架):不内嵌引擎、三层模型(Provider/Agent/Session)+ 两 provider 形态(process/connector)+ 能力驱动 UI + Zustand store(LIVE_SLOT 升级/懒加载历史/3s 重连/P0 只渲染 text·complete·error)+ 组件 + Roadmap P0-P4 + 未完成项。两页 source 均核对实现(`serve.py` + `serveClient.ts` + `store.ts`;桌面结构经 Explore agent 实测:electron-vite 三段式、contextIsolation:true、无 ipcMain、preload stub)。total_pages 24→26。
- **记录待办**: 桌面思考块对 glm-5.2-zp 休眠 —— 直连 GLM 原始 SSE 探针(绕开 serve+agent,`<内部网关IP>/public/v1`)确认默认调用不发任何 reasoning 字段(delta 仅 content+role;非字段名不符/非网关剥离),推理在 content 里。待办(换 reasoning 模型 / 验证 `thinking` 参数)入 [[active]] 后续方向;详见 [[0025_desktop-control-plane]]。探针脚本:OS temp `probe_glm_raw.py`(WS 探针 `probe_reasoning.py`)。

## 2026-08-07

- **回写**: 多实例配置(`--config` 启动时选实例)→ 新增概念页 [[0023_multi-instance-config]]。含:一个 YAML 描述一个实例(`agent_home`→数据根隔离 + 最高优先级配置覆盖)、peek argv 设计(import 时扫 `sys.argv` 算对 `AGENT_HOME`,0 下游重构;否决函数化 ~30 touch-point,理由:与读 `AGENT_HOME` env 同质)、优先级链(默认<用户 yaml<项目 yaml<`--config` yaml<`.env`)、`.env` 浮顶陷阱(7 个 env-aliased key `model`/`api_key`/`base_url`/... 须放 sibling `.env` 非 yaml,实测 yaml 的 `model` 被仓库根 `.env` 的 `MODEL` 压)、数据隔离边界(13 个常量消费者零改动)。落地 4 文件(`config.py`/`app.py`/`chat.py`/`server.py`)+ `CLAUDE.md` 同步,12 测试全绿 + smoke。total_pages 23→24。
- **回写**: 测试策略分层模型 → 新增概念页 [[0022_testing-strategy]]。含 L0-L4 分层 + 注入假模型 client 技巧(`__init__` 加 `client=None`,向后兼容) + `FakeChatClient`(`SimpleNamespace` 仿 SDK 形状,对照 `_non_streaming_call`/`_streaming_call` 逐属性确认) + 测试隔离(monkeypatch `_DB_PATH`、`is_subagent=True` 跳 auto-title、`system_prompt_override` 跳 prompt 构建) + 行业对照(mock client / LLM-judge / 轨迹评估 / 沙箱 scorer)。同日落地 `tests/` 骨架(pyproject+requirements 加 pytest、conftest/fakes、4 个 test_*.py、12 测试全绿),`test_max_iterations_sets_exit_reason` 直接保护上轮 `_last_exit_reason` 改动。total_pages 22→23。

## 2026-07-31

- **回写**: CLI 交互重写 hybrid TUI → 新增变更页 [[0021_cli-hybrid-tui]](type=change)。含 hybrid REPL(ptk+msvcrt)、cwd 注入+缓存失效、fresh-by-default、steer 自动续跑、CODING_GUIDANCE 7 点、日志 file_level、⏺ 工具开始打印、prompt_toolkit+textual 依赖。踩坑(prompt_toolkit patch_stdout 乱码、Textual Windows 输入不工作、Rich Console 清行)全记。total_pages 21→22。

## 2026-07-30

- **回写**: KBS 子系统(可插拔业务知识库协议)→ 新增变更页 [[0019_kbs-feature]](type=change,功能点)。含:一个开关管两件事(`enabled` 门控前导注入 + `check_fn` 门控工具)、精简前导(不内联整段 AGENT.md,per-session 缓存 + 前缀缓存)、`kbs_init` 做成工具(确定性盖章器,非 CLI 子命令/非 LLM 徒手建)、复用 core 文件工具只加 3 个专用工具、`kbs_search` 证据驱动后加(日志实测 agent 绕过 index 直接 grep → 加 index-first 工具)。补 [[0001_xihe-agent]] 总览功能点。total_pages 19→20。

- **回写**: 会话管理命令 → 新增变更页 [[0020_session-management-commands]](type=change,功能点)。含 `/sessions`(gateway 按 user 过滤+隐藏内部)、`/history [N]`、`/resume`(会话内切换 CLI only)、`xihe chat -r`、后端 `list_sessions(...,user_id,include_internal)`。机制要点:resume 复用 session_key、会话内切换靠 `cmd_ctx["cli_source"]`、`if user_id:` 避开 SQL NULL 坑。补 [[0006_session-design]] 交叉链接。total_pages 20→21。

## 2026-07-22

- **回写**: 工具/技能/角色分层架构 → 新增概念页 [[0018_tool-skill-workflow-role-layering]]。含三层（tool/skill/角色）+ workflow 是 skill 编排用法（调研后放弃单独 workflow 机制，skill 已够）+ 关系图 + 角色配置 + MCP 分组。total_pages 18→19。**订正** [[0017_role-based-subagents]] 子 agent tool 边界（`subagent_blocked` 标签替代 toolset+tool block，`is_subagent` 构造属性替代 `delegate_depth>0` 判断，`skill_view`/`skills_list`/`todo` 放出）+ 补 [[0018]] tool 标签 + is_subagent + 补 [[0017]] 主/角色完整对照表 + 三设计取舍。**回退角色化**（路由负担/隔离副作用/token/边界模糊）——删 `roles.py` + `agents/` + delegate role + MCP 分组 + skill 精简 + ssh 回 DEFAULT_TOOLSETS。[[0017]]/[[0018]] 标注废弃。

## 2026-07-21

- **回写**: 角色化子 agent 设计与落地 → 新增概念页 [[0017_role-based-subagents]]。含角色定义格式（agents/*.md，mcp 独立字段）、加载机制（照搬 skill，修正 user/bundled 优先级 bug）、主子能力统一 vs 定位区分、role_mode 放宽父交集、skill 索引精简 + MCP 按角色分组、缓存一致性、对 cron/skill_manage 的影响。首个角色 web-ops。total_pages 17→18。

## 2026-07-20


## 2026-07-17

- **回写**: 任务运行时中断/停止/steer 控制通道设计 → 新增概念页 [[0016_interrupt-stop-steer]]。含 per-agent 中断、`interruptible_iter`、子进程注册表、steer 重排/丢弃语义、以及调试过程中踩的 7 个坑。total_pages 16→17。

## 2026-07-16

- **回写**: agent 安全（主人身份/私密信息防护）调研 → 新增 insight [[0009_agent-security-master-identity]]。total_pages 15→16。
- **回写**: SSH 远程访问设计 → 新增概念页 0015_ssh-remote-access（已脱敏移除）。total_pages 14→15。

## 2026-07-13

- **回写**: 项目上下文加载规则 → 新增概念页 [[0014_project-context-loading]]。total_pages 13→14。

## 2026-07-10

- **回写**: toolset scope 设计（分层 + request_tools + 设计决策）→ 新增概念页 [[0013_toolset-scope-and-dynamic-expansion]]。total_pages 12→13。

## 2026-07-09

- **回写**: 新增 http/maven_dep/node_version 工具 + web 工具 check_fn 门控 + gateway/file/mcp/cron 增强 → 新增变更页 [[0012_new-tools-and-gateway-enhancements]]。total_pages 11→12。

## 2026-08-21

- **清理**: 按用户要求删除外部对比项目相关全部记录——删除 insight 0008 与对应 raw 快照；变更页 0010 改名 0010_cdp-default-and-cron-job-forms 并剥离对比表述（CDP/cron/提示词三组真实变更保留）；全部入链页面与 raw 快照同步去引用。total_pages 39→38、sources 7→6。

## 2026-07-06

- **回写**: 沉淀 gateway 架构设计 → 新增概念页 [[0011_gateway-architecture]]（SharedContext 拥有重对象、每消息 `create_agent()` 建薄 agent、SQLite 续对话、模块全局共享状态、阻塞 join 并发模型 + 并发化需加 per-session guard 的隐患）。total_pages 10→11。

## 2026-07-03

- **回写（已验证）**: 浏览器 CDP 默认 + cron job 多形态 + 提示词约定 → 新增变更页 [[0010_cdp-default-and-cron-job-forms]]、新概念页 [[0009_cron-jobs]]、订正 [[0003_browser-tools]]。
- **整理**: 订正 0003 过时的「persistent 为主」认证框架（已不属实，会误导排查方向）。
- **元数据**: total_pages 8→10，last_writeback/lint 更新。

## 2026-07-01

- **收录**: `.docs/` 7 篇 → `raw/sources/` 快照 + 6 个 concept（0002-0007）。
- **整理**: 删除原 `.docs/` 目录（内容已迁入 wiki）。
- **初始化**: 创建 wiki 目录骨架与入口页面（active / recent / index / log）。
- **初始化**: 创建 `meta/`（lint-status.json、candidates/、schemas/）与 `raw/sources/`。
- **收录**: 基于 `CLAUDE.md` 创建种子页面 `wiki/entities/0001_xihe-agent.md`（项目总览）。
