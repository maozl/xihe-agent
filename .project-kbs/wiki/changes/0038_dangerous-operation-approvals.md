---
type: change
title: 危险操作审批落地（三值权限管线 + 三模式审批交互）
slug: 0038_dangerous-operation-approvals
change_type: feature
risk_level: medium
status: completed
created: 2026-08-20
updated: 2026-08-20
affected_services:
  - xihe-agent
affected_modules:
  - tools/_approvals.py (新增)
  - tools/__init__.py
  - tools/terminal.py
  - tools/delegate_tool.py
  - tools/specialist_agent_tool.py
  - core/agent.py
  - gateway/serve.py
  - gateway/server.py
  - cli/chat.py
  - desktop/src/renderer/src/lib/serveClient.ts
  - desktop/src/renderer/src/store.ts
  - desktop/src/renderer/src/components/ChatPanel.tsx
  - config.example.yaml
  - tests/test_approvals.py (新增)
---

# 危险操作审批落地（三值权限管线 + 三模式审批交互）

## 摘要

补全 terminal.py 里的死代码审批分支（`XIHE_GATEWAY_SESSION` env 从未设置、manual 下照样直跑），做成完整审批系统：三值决策管线（allow/ask/deny，借鉴 Claude Code 权限系统）+ dispatch 单汇聚点门 + XiheAgent 阻塞等待协调 + CLI/gateway/serve-desktop 三模式审批交互 + 桌面审批卡（含"批准，不再询问"第三按钮）。架构沉淀见 [[0037_approval-permission-system]]。

## 变更内容

### 判定层（tools/_approvals.py 新增）

- `_DANGEROUS_PATTERNS`（30 条正则）+ `detect_dangerous_command` **从 terminal.py 移入**（terminal 反向 import，无循环）；归一化链：strip ANSI → 去 null → NFKC → lower。
- `_HIGH_RISK` 高危参数表 7 类：ssh_exec（恒定）/ process(stop) / browser_logout(wipe_profile) / skill_manage(delete/remove_file) / kbs_init(force) / cronjob(delete) / node_version(install/uninstall)。每条中文 summary 含关键参数。
- `needs_approval(name, args, config) -> (bool, summary)`：mode auto 恒放行；terminal 走正则；其余查表。
- 三值升级（借鉴 Claude Code，第二轮）：`evaluate() -> ('allow'|'ask'|'deny', summary)`，优先级 mode auto > deny 规则 > allow 规则 > 会话记忆 > 危险判定；`rule_text`（terminal/ssh_exec 取命令原文，其余 "action 关键参数"）+ `_rule_matches`（`"tool(glob)"` 语法，fnmatch，括号贪婪到尾）；`_SESSION_RULES` 会话记忆 + `remember_rule`。
- `parse_approval_reply`（True/False/"always"/None，整词匹配，**always 词先查**——"a" 不在批准词集里）；`try_resolve_steer(agent, text)` 三入站口共用折批复。
- **会话记忆类级化（同日修正）**：`_danger_detail(name, args)` 抽出危险类判定（terminal=命中模式描述 / 高危表=工具名）；`remember_rule(..., config)` 记 `(tool, 精确文本)` + `(tool, "danger:类键")` 双键；evaluate 记忆命中查两类键；dispatch 传 agent config。测试：`test_session_memory_scope_and_danger_class`（同类换目标 allow / 换类与换工具仍 ask / 跨会话隔离）+ `test_session_memory_high_risk_tool_whole_class`。
- **LLM 语义判定层（次日落地，用户裁决"正则枚举覆盖不全，加一层 LLM 判定"+ 默认开；prompt 二轮采纳用户稿：effect 字段/三档规则/反混淆行）**：`_SUSPECT_RE` 漏斗（删除/破坏动词、提权、远程执行、系统区域，NFKC 归一化）→ `_llm_judge_command` 单次 `aux.call_llm("approval_judge")`（10s/temperature 0/200 tokens，prompt 命令当纯数据防注入、三档评分+拿不准判 warning 防审批疲劳、要求识别实际行为/还原混淆意图）→ 严格 JSON `{risk(safe|warning|dangerous，仅 dangerous 弹审), category(固定枚举), reason, effect}`，effect（命令实际作用）进审批摘要；fail-open（未接 aux/失败/解析失败 → 放行 + warning 日志）；类键 `danger:llm:{category}` 经 thread-local 传入 remember_rule；`evaluate(+aux)` 尾部级联，dispatch 传 `agent.aux`；`approvals.llm_judge` 默认开，模型可配 `auxiliary.approval_judge.model`；`_DEFAULT_TIMEOUTS` 加 approval_judge:10。测试 +8（漏斗不触发/ask+effect/三档放行/垃圾输出 fail-open/配置关/同类记忆/异类仍问）。

### 协调层（core/agent.py）

- `_approval_shared` dict（pending/lock/request_cb/result_cb/session_key）。
- `chat()` 加 `approval_request_callback`/`approval_result_callback` 参数（与 stream_delta 等现有回调同模式）；**仅顶层 chat 写 session_key**（`if not self.is_subagent`）。
- `request_approval(tool, summary) -> (approved, reason, always)`：五结局状态机（批复/超时按 timeout_action/中断/无回调立即拒/回调异常拒），保守失败；`resolve_approval(id, approved, note, always)`；`pending_approval` property。

### 拦截层（tools/__init__.py dispatch）

- handler 执行前单门：`parent_agent` 为 None（cron no_agent 脚本、测试直调）跳过；deny → `tool_error(..., blocked=True, approval=...)` 即拒；ask → 阻塞等待；always 批准 → `remember_rule`。

### 子代理共享（delegate_tool.py:150 / specialist_agent_tool.py:77）

- 构造后 `child._approval_shared = parent_agent._approval_shared`——resolve 打通到任意委派深度；回调 stash 仅非 None 时写（子 agent 不清父回调）。

### 三模式通道

- **serve**：`Emitter.on_approval_request/on_approval_result`（WS `approval_request`/`approval_resolved` 事件）；WS 分发加 `approve` 命令（含 always flag）；`_steer` 开头 `try_resolve_steer`；`_capabilities` 加 "approvals"。
- **gateway**：`handle_message` 注入回调（`run_coroutine_threadsafe` 跨线程发确认消息）；`steer_session` 先折批复。
- **CLI**：`handle_during_turn` else 分支先 `try_resolve_steer`（两种 REPL 都经过）；`run_turns_threaded` 注入打印回调。

### 桌面（desktop/）

- `serveClient.ts`：ServeEvent 联合加 approval_request/approval_resolved；`approve(convId, id, approved, always?)`。
- `store.ts`：Message.pendingApproval（pending/approved/denied/expired 四态）；complete/error 把残留 pending 置 expired（socket 先掉/回合先结束的卡片读作未答复）；approve action 乐观置终态。
- `ChatPanel.tsx`：ApprovalCard（amber 卡 + 批准 / **批准，不再询问** / 拒绝 三按钮；settled 只显徽标；回合结束按钮变"等待答复（无法批复）"提示）。

### 文档与配置

- `config.example.yaml` approvals 段：mode/timeout/timeout_action/deny/allow/llm_judge 六键 + 决策顺序与规则语法注释。
- terminal.py 删除 `off` 检查与 `XIHE_GATEWAY_SESSION` 死分支。

## 设计迭代（用户裁决记录）

1. **范围**：terminal 危险命令（复用 30 条正则）+ 高危工具（删除/远程/破坏类）；**超时行为可配置**（`timeout_action: deny|allow`，默认 deny）。
2. **三模式全做**审批交互（CLI/gateway/serve-desktop）。
3. **借鉴 Claude Code**：用户问"claude 里面怎么做审批，借鉴下"，AskUserQuestion 三档选了**完整借鉴**（三值管线 + 配置规则 + 会话记忆 + always 通道 + 桌面第三按钮）。
4. 测试阶段用户指示"测试先不用写"，后补齐（见下）。

## 边界与已知取舍

- 并行路径（read_only 组）不含审批工具 → 顺序执行 → 单 pending 不冲突；不做多 pending 队列。
- `delegate_task`/`run_<slug>_agent` 本身不拦——拦的是子代理内部真正落地的危险调用（经共享 `_approval_shared` 提到同一 UI）。
- cron 跑 agent 无 UI 回调 → 立即拒绝并给模型明确错误（保守正确；要自动跑危险命令设 `mode: auto`）。
- 会话记忆（初版）精确匹配是防滥用取舍：批准 `rm -rf /tmp/a` 后模型发 `/tmp/b` 仍要问。**同日推翻**：模型生成的命令文本每次内嵌不同目标名，精确匹配等于每条重问（回收站连删三问实测）→ 升级为**类记忆**（精确文本 + `danger:类键` 双键，同一危险模式/同一高危工具本会话不再问，换类/换工具仍问）。
- **文本层匹配可绕过**（变量间接/base64 拆分）——启发式安全网定位，与 Claude Code 同限，详见 [[0037]] 诚实边界节。
- **2026-08-20 事故补齐（落地当天实测）**：用户让 agent 删回收站文件，`powershell Remove-Item -LiteralPath 'E:\$RECYCLE.BIN\...' -Recurse -Force` **直过未审批**——原 30 条模式 Unix 为中心，Windows 删除命令整体缺口。补 7 条（`remove-item … -recurse` / `rd|rmdir|del|erase` + `/s` / `clear-recyclebin` / `format x:`），30→37；PowerShell 的 `rm` 别名本就被 `\brm\s+-[^\s]*r` 覆盖。单文件删除（`Remove-Item a.txt`）仍不拦——与 Unix `rm a.txt` 对称的取舍。**同日第二事故**：用户拒绝 logo.png 后模型 reasoning "单文件不需要递归" 改写为非递归 `Remove-Item -Force` 重发，不命中任何模式直过删除——补「回收站枚举签名（`Namespace(0xA)`/`$RECYCLE.BIN`）× 删除动词」双向 2 条定向模式兜住该形态，37→39。

## 验证

- `tests/test_approvals.py` 新增：L0 needs_approval（terminal 危险/安全/auto/缺省 manual + 高危表 15 参数化）、L1 dispatch 门（拒绝拦 handler/批准放行/无 parent_agent 跳过/安全命令不问）、协调层（批准/拒绝/超时 deny/超时 allow/中断/无回调/回调异常/二 pending 拒绝/result 回调）、L0 规则（deny 即拒不弹窗/deny 压 allow/bare 工具名/action 文本/会话记忆精确+跨会话隔离+deny 压记忆/always 词集/steer always/request always flag/dispatch 端到端 always→第二次免问）。
- 全量 `pytest tests/ --ignore=tests/evals` exit=0。
- 桌面 `npm run build` 绿。
- 踩坑：deny 规则示例 `"terminal(* mkfs *)"` 要求 mkfs 前有空格，命令 `mkfs /dev/sda` 开头即命中失败——改 `*mkfs*`（test 与 config.example.yaml 同步修）。

## 相关页面

- [[0037_approval-permission-system]] — 本变更沉淀的稳定架构（四层/三值管线/规则语法/会话记忆/诚实边界）
- [[0002_tool-registry-and-dispatch]] — dispatch 汇聚点（拦截层的基础设施）
- [[0016_interrupt-stop-steer]] — steer 通道与中断（批复折算/中断感知复用的管道）
- [[0024_desktop-serve-protocol]] — WS 契约（approval 事件对）
