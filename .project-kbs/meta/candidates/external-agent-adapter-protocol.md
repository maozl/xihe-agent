---
type: candidate
title: 候选: 外部 agent 适配器协议对照（claude + codex）
slug: external-agent-adapter-protocol
status: promoted
created: 2026-08-12
updated: 2026-08-26
resolved_at: 2026-08-26
resolution_target: wiki/concepts/0040_external-agent-adapter-protocol.md
resolution_note: codex 本机实测通过 + 双引擎 driver 落地内核后提升为正式 concept，与 [[0028]] 组「外部 agent 接入协议」双页。剩余开放点（能力共享边界、app-server 长驻）随页转入 [[0040]] 开放点节。
related_topic: 0028_desktop-claude-transport-architecture
derived_from:
  - wiki/concepts/0028_desktop-claude-transport-architecture.md
  - wiki/concepts/0029_desktop-dual-engine-architecture.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
why_it_matters: 决定「通用 external-agent adapter」是否成立，以及 codex 能否接入当前 airgap 环境。是 xihe 把 claude/codex 作为能力补充时的传输层基线；实测已通过且双引擎驱动已落地（2026-08-26），已提升为 [[0040_external-agent-adapter-protocol]]。
next_action: （已决议）正式参考页见 [[0040_external-agent-adapter-protocol]]；开放点（能力共享边界 / app-server 长驻优化）在 0040 开放点节跟踪。
---

# 候选: 外部 agent 适配器协议对照（claude + codex）

## 摘要

调研 codex CLI 的 headless 接入协议（`codex exec --json`），与已实测的 claude stream-json（[[0028]]）做契约级对照。**结论：两者同构度高，通用 external-agent adapter 成立**——都是 headless 子进程 + NDJSON 事件流 + 会话 id 冷 resume + 基于 cwd + 环境变量注入凭据 + 只能杀进程中断。关键差异在生命周期模型（claude 可长驻多轮 stdin / codex 每轮新进程）与事件 schema。

> ✅ **2026-08-26 本机实测通过（codex 0.146.1 + 内部网关），双引擎 driver 已落地内核**（见下节）。原「未在本环境实测」的最大风险项——内部网关 Responses API 兼容性——已消除：网关是 litellm，对 codex 的 Responses-API 请求实测可用（SSE 流式、store:false、加密 reasoning、function tools、web_search tool、resume 时 prompt caching 命中），**无需** `wire_api="chat"` 降级。实测挖出两个原调研未见的硬坑（multi_agent namespace 工具 500、Windows elevated sandbox 1385），均已定位并修复。

## 2026-08-26 本机实测与落地

**环境**：codex 0.146.1（npm shim → win32-x64 vendor exe），内部网关 `http://<内部网关IP>/public/v1`（litellm），模型 glm-5.2-zp，`~/.codex/config.toml` 配 `[model_providers.zp] wire_api="responses"`。

**硬坑 1（决定性）：`--disable multi_agent` 必带。** codex 0.146 默认开 `[features] multi_agent`，请求里带 `{"type":"namespace","name":"multi_agent_v1"}` 工具 → litellm 返回 500 "Unsupported tool type: namespace"。codex 把所有可重试错误包成 **"We're currently experiencing high demand"** 重试 5 次后 `turn.failed`——报错信息完全误导（不是负载问题）。定位方式：mitm 代理抓包 + 原样重放确认。修复：spawn 恒加 `--disable multi_agent`（或 config.toml `[features] multi_agent=false`）。

**硬坑 2：Windows sandbox 模式。** `[windows] sandbox` 只认 `elevated|unelevated`（无 `disabled`）。`elevated` 走 CreateProcessWithLogonW（LOGON32_LOGON_BATCH），需要 CodexSandboxOffline/CodexSandboxOnline 账号有 SeBatchLogonRight——本机未授予，报错 1385，**所有 shell 命令失败**。`unelevated`（当前用户 + 受限 token）实测正常。sandbox 用户密码存 `~/.codex/.sandbox-secrets/`（DPAPI），展示时须掩码。

**stdin 语义（与 claude 相反）**：prompt 走 argv 时若 stdin 是管道，codex 仍阻塞等 stdin EOF（把 stdin 内容追加为 `<stdin>` 块）。driver 必须 **prompt 写 stdin 后立即 close**——EOF 是 boot 门。resume 形态实测：`codex exec [flags] resume <thread_id> -` 同样从 stdin 读 prompt，一种 argv 形状通吃冷热。

**凭据实测**：`CODEX_API_KEY` 优先级最高（> auth.json > OPENAI_API_KEY 故意降级）确认。**base_url 不吃 env**——自定义 provider 的 base_url 只认 `~/.codex/config.toml` 的 `[model_providers.<id>]`；`OPENAI_BASE_URL` 只作用于默认 openai provider（还会 append `/responses`）。所以 xihe 对 codex 只注入 api_key，provider/model/wire_api 全留 config.toml。

**落地（已合入内核，未单独提交）**：`src/core/external_agent.py` 重构为**共享机制层**（`_resolve_bin`/`_routed_argv`（.cmd→cmd.exe 路由 WinError 193）/`_spawn_cli`/`_start_stderr_drain`/`_TreeKillHandle` 中断注册/PID 文件带 engine 字段 + 按引擎指纹孤儿清扫/`(engine, session_key)` 双键 resume 表——防同会话交替两引擎串线）+ 两个生命周期策略：`ClaudeDriver`（WARM 长驻，行为不变）与 `CodexDriver`（ONE-SHOT：每轮 `exec --json --skip-git-repo-check -C <cwd> --disable multi_agent [-m] [-s|bypass 旗标] [resume <tid>] -`，`thread.started` 捕 tid，`Reconnecting...` 顶层 error 只挂起不终局，轮末 kill_tree + 摘 PID 不留跨轮进程）。`external_agent_tool.py` 双引擎泛化（`_ENGINES` 对称：bin 检查/凭证/permission_mode 默认 codex=`workspace-write`；codex 项目约定文件是 **AGENTS.md** 非 CLAUDE.md）。验证：`tests/test_external_agent_codex.py` 11 项 hermetic（argv 契约/stdin 关闭/全事件映射/turn.failed/Reconnecting 噪声/resume 隔离/中断清理）+ 全量 pytest 344 绿 + 真实 E2E 两轮（16s 完成 shell 执行 + resume 记忆成功）。

## codex CLI headless 协议要点（0.146.1 实测校订）

**入口**：`codex exec [PROMPT]`（别名 `codex e`）。prompt 走 argv 或 stdin（`-` 占位 / 无 argv 自动读）。跑到 `turn.completed` 进程即退——**一次性（one-shot）**，无 claude 那种长驻多轮 stdin。**需 git 信任目录**或 `--skip-git-repo-check`。

**输出**：`--json`（别名 `--experimental-json`）→ stdout 是 JSONL，每行一个 `{type:...}` 事件。完整事件清单：

| 类别 | 事件 | 关键字段 |
|---|---|---|
| 顶层 | `thread.started` | `thread_id`（= 会话 id，resume 用） |
| | `turn.started` | — |
| | `turn.completed` | `usage.input_tokens/cached_input_tokens/output_tokens` |
| | `turn.failed` | `error.message` |
| | `error` | `message`（瞬态 `Reconnecting... X/Y` 视为非致命） |
| item 级 | `agent_message`（仅 completed） | `item.text`（最终文本） |
| | `reasoning`（若启用） | `item.text` |
| | `command_execution` | `item.command/aggregated_output/exit_code/status`（output 截断 64KiB） |
| | **`file_change`**（仅 completed） | `item.changes[].path/kind`（add/delete/update） |
| | `mcp_tool_call` | `item.server/tool/arguments/result/error/status` |
| | `web_search` | `item.query` |
| | `todo_list` | `item.items[].text/completed` |

`item.id` 稳定，started/updated/completed 复用同一 id。成功信号 = `turn.completed`；失败 = `turn.failed`；item 级看 `item.status`。exit 0 = 成功 / 1 = 失败。

**续接（冷）**：`codex exec [flags] resume <thread_id> -`（prompt 走 stdin，实测）/ `resume --last`——新进程 + resume，`thread_id` 复用、token 累积、`cached_input_tokens` 命中。**这正是 claude 重写前（[[0027]]）的「每轮新进程 + --resume」模式。**

**凭据/模型**（实测优先级）：`CODEX_API_KEY` > `~/.codex/auth.json` > `OPENAI_API_KEY`（**故意被降级，别用**）。`CODEX_HOME` 重定向数据根（≈ xihe 的 `AGENT_HOME`）。`-m/--model` 选模型；`config.toml` 定义自定义 `[model_providers.xxx]`（`name` + `base_url` + `wire_api`；**base_url 只认此处不吃 env**）。

**cwd**：`-C/--cd <dir>`、`--add-dir` 扩展 sandbox 可写。和 claude 一样基于 cwd，**workspace 共享天然**。

**sandbox/权限**（`exec --help` 原文核实）：`-s/--sandbox` 枚举仅 **`read-only | workspace-write | danger-full-access`** 三值——**没有 claude 式 `bypassPermissions` 值**；等价物是独立旗标 `--dangerously-bypass-approvals-and-sandbox`。xihe 侧把 `bypassPermissions` 作跨引擎别名映射到该旗标。Windows 另有 `[windows] sandbox=elevated|unelevated`（见硬坑 2）。exec 模式强制 `approval: never`（无 TTY）。`-a/--ask-for-approval`、`--search` 是**全局 flag**（须在 `exec` 前）。

**中断**：exec 无协议级中断，只能杀进程（同 claude 冷路径）。

**MCP**：codex 自己作 MCP **client**，`config.toml` `[mcp_servers.x]`。改 MCP 配置要重启。`mcp_tool_call` 事件能在 JSONL 里看到 codex 调 MCP 工具。

**补充**：`codex app-server` = JSON-RPC 2.0 双向协议（类 MCP），可能是 codex 的「长驻双向」方案——当前 one-shot 每轮 ~2-3s 启动开销实测可接受（一轮总 16s，大头在网关往返），**暂无需求**；若将来有延迟痛点再验证。

## claude vs codex 协议对照（adapter 视角）

| 维度 | claude（[[0028]] 已实测） | codex（**2026-08-26 已实测**） | adapter 处理 |
|---|---|---|---|
| headless 入口 | `claude -p` | `codex exec` | 各一份 spawn 配置 |
| 输出 | NDJSON（stream-json） | NDJSON（`--json`） | **同形** ✓ |
| 生命周期 | **长驻**（一会话一进程，stdin 跨轮） | **一次性**（每轮新进程） | adapter 抽象两种 lifecycle policy（已落地） |
| 多轮续接 | 热路径 stdin + 冷 `--resume` | 只有冷 `resume <thread_id>` | codex = claude 重写前的成熟模式 |
| 会话 id | `session_id` | `thread_id` | 都从首个事件捕获；**必须按 (engine, session_key) 双键存** |
| 事件 schema | `stream_event`/`result`/`system/init` | `thread.*`/`item.*` | 各一份 NDJSON→ServeEvent mapper |
| cwd/workspace | pin cwd | `-C`/`--cd` | 统一 ✓ |
| 凭据 env | `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` | `CODEX_API_KEY`（**base_url 只认 config.toml**） | 各一份 env 注入 |
| 就绪信号 | `system/init`（**有 stdin-boot 死锁**，见 [[0029]]） | `thread.started`（无 boot 死锁，但 **stdin EOF 是 boot 门**——反方向的坑） | 各自硬化 |
| 中断 | 杀进程 | 杀进程 | 统一 ✓ |
| 文件改动可见性 | 无结构化事件 | `file_change` 事件（path/kind） | codex 多一个亮点 |
| 平台坑 | .cmd 包装（WinError 193） | 同左 + `--disable multi_agent` 必带 + Windows sandbox 模式 | `_routed_argv` 共用；codex 两坑进 spawn 常量 |

## adapter 设计影响

1. **两种 lifecycle**：claude 长驻 / codex 每轮新进程。**已按此落地**（ClaudeDriver WARM / CodexDriver ONE-SHOT，共享机制层共载）。
2. **每个 agent 一份 NDJSON→ServeEvent mapper**：codex 映射已落地——`agent_message`→text/complete、`command_execution`/`mcp_tool_call`/`file_change`→tool_call/result、`reasoning`→thought、`turn.completed`→complete、`turn.failed`/`error`→error。
3. **codex 多一个亮点 `file_change`**：已映射为 tool_call + 立即 tool_result（无独立 result 事件，不发会留永远转圈的 trace 行）。
4. **codex 的 stdin 语义与 claude 相反**：claude 靠第一行 stdin 触发 boot 且 stdin 长驻；codex 把 stdin 当 prompt 源、**EOF 才开跑**——driver 写完必须 close。
5. ~~airgap 网关风险~~ → **已消除**（2026-08-26）：网关（litellm）接受 Responses API 请求，`wire_api="responses"` 直连可用。但代价是 **`--disable multi_agent` 硬要求**（namespace 工具 500，见硬坑 1）。
6. **codex 需 git 信任目录**：spawn 恒加 `--skip-git-repo-check`。

## 已决：接入模式 = IPC（2026-08-12）

把 claude/codex 作为 xihe 的能力补充，**定 IPC（原生 headless 子进程驱动，即 [[0028]] ClaudeRunner 路子），不走 MCP**。决策理由：

1. **语义契合（决定性）**：agent 是长时/有状态/多步/流式/可中断的，MCP 的 tool-call 请求-响应语义与之根本错配（阻塞/超时/无流式/无中断，事件粒度丢失）；IPC 用原生 NDJSON 直连是 1:1 契合，事件一次映射成 ServeEvent 不丢粒度。
2. **产品语义是「借脑」**：会话级协作（把子任务整个托付给 claude），是 delegate 语义而非单步工具；xihe 已有 delegate 机制可挂接（[[0017]] 角色化回退但 delegate 保留）。
3. **复用**：ClaudeRunner 桌面端已写且实测；codex 同构（每轮新进程 = ClaudeRunner 重写前模式），adapter 抽象后两者共载，新增 codex 近似套模板。**（2026-08-26 已验证此预判：CodexDriver 即套模板 + 两个平台坑）**
4. **进程开销不构成选 MCP 的理由**：开销来自 agent 本身，换 MCP 省不掉；控进程靠生命周期管理。活跃会话 ≈ 同时 1-2 个 agent 进程，空闲即回收；codex 本就 one-shot，轮末即清。

**混合暴露**：传输层 IPC 做重活；触发层既可 delegate 命令，也可薄封装成一个 MCP tool 给 xihe 自己的 agent 在 tool-calling 里选——两者不矛盾。MCP 的标准化价值留给「对外（非 xihe 的 MCP client）提供能力」的未来场景，当前不做。

## 待验证事项

1. ~~codex 在内部网关连通性~~ → **已验证通过**（2026-08-26）：Responses API 直连可用；反而新增 `--disable multi_agent` 硬要求（见硬坑 1）。
2. ~~codex exec 一次性模型的延迟~~ → **已验证可接受**：每轮新进程 ~2-3s 启动 + 网关往返，实测一轮 16s；app-server 长驻方案挂起，无痛点不动。
3. ~~接入模式定夺：MCP vs IPC~~ → **已决 IPC**（2026-08-12，见上）。external-agent driver 已落 **xihe 内核层**（`src/core/external_agent.py`，双模式 + 桌面共用）。
4. **能力共享边界（仍开放）**：claude/codex 接入后能否/如何用 xihe 的 skill/MCP/内部工具（IPC 模式下可注入 prompt 或回调；MCP 模式靠 reciprocal）。当前外部引擎只有自己的 file/shell 面，无 xihe 工具。

## 相关页面

- [[0028_desktop-claude-transport-architecture]] — claude 接入架构（已实测，正式参考页），本候选的 claude 侧基线
- [[0029_desktop-dual-engine-architecture]] — 桌面端双引擎总览 hub（含 ClaudeRunner 生命周期硬化 5 项）
- [[0027_desktop-claude-longlived-rewrite]] — claude 长驻重写；其「重写前」逻辑（每轮新进程 + resume）= codex 的接入模式
- [[0026_desktop-agent-model-built-in-xihe]] — Agent = 类型（内置 xihe + 可添加 claude）；本候选是「可添加」的传输层细化
- [[0005_mcp-dynamic-registration]] — xihe 的 MCP client 机制（MCP 模式若选则复用此）
- [[0017_role-based-subagents]] / [[0018_tool-skill-workflow-role-layering]] — delegate 机制（IPC 模式若选则挂接此；注意角色化已回退，delegate 机制保留）

## 来源

- **本机实测**：codex 0.146.1 + 内部网关（litellm），2026-08-26；mitm 抓包复现定位 multi_agent namespace 500；`codex exec --help` 核实 sandbox 枚举与旗标；E2E 两轮验证 resume。修复落地 `src/core/external_agent.py` / `src/tools/external_agent_tool.py` / `tests/test_external_agent_codex.py`。
- [Codex exec --json event cheatsheet — takopi](https://takopi.dev/reference/runners/codex/exec-json-cheatsheet/)（完整事件类型清单）
- [Codex CLI exec mode experiments: 81 flag/feature tests — alexfazio gist](https://gist.github.com/alexfazio/359c17d84cb6a5af12bac88fa1db9770)（codex 0.114.0 实测，2026-03-13）
- [Non-interactive mode — ChatGPT Learn（官方）](https://learn.chatgpt.com/docs/non-interactive-mode)（`--json` JSONL 官方说明）
- [Codex CLI config/mod.rs — Fossies 源码](https://fossies.org/linux/codex-rust/codex-rs/core/src/config/mod.rs)（exec 强制 `Never` approval）
- [capo-codemode — lib.rs](https://lib.rs/crates/capo-codemode)（第三方 shell-out 印证一次性调用模式）

## 决议

- **结果**: promoted（2026-08-26）
- **目标**: [[0040_external-agent-adapter-protocol]]
- **理由**: codex 本机实测通过（Responses API 直连可用，原最大风险消除）+ 双引擎 driver 已落地内核（`src/core/external_agent.py`，pytest 344 绿 + E2E 两轮含 resume），候选内容全部转正为稳定协议参考；与 [[0028]] 组「外部 agent 接入协议」双页。
- **未尽事项**: 能力共享边界（外部引擎用 xihe skill/MCP）与 app-server 长驻优化 → 转入 [[0040]] 开放点节跟踪。
