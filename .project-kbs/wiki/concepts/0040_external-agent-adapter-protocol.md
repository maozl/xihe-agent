---
type: concept
title: 外部 agent 适配器协议（claude + codex 双引擎驱动）
slug: 0040_external-agent-adapter-protocol
aliases:
  - external-agent adapter
  - codex 接入协议
  - 双引擎外部 agent 驱动
tags:
  - architecture
  - external-agent
  - claude
  - codex
  - transport
status: active
created: 2026-08-26
updated: 2026-08-26
related_pages:
  - wiki/concepts/0028_desktop-claude-transport-architecture.md
  - wiki/concepts/0029_desktop-dual-engine-architecture.md
  - wiki/changes/0027_desktop-claude-longlived-rewrite.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/concepts/0005_mcp-dynamic-registration.md
sources:
  - path: 本机实测 codex 0.146.1 + 内部 litellm 网关（含 mitm 抓包复现）
    date: 2026-08-26
---

# 外部 agent 适配器协议（claude + codex 双引擎驱动）

## 摘要

xihe 把本地 `claude` / `codex` CLI 当作**可委派的外部 agent 引擎**接入（`run_external_agent` 工具 → `src/core/external_agent.py`）。两引擎的 headless 协议同构——子进程 + NDJSON 事件流 + 会话 id 冷 resume + 基于 cwd + env 注入凭据 + 杀进程中断——因此**一个适配层共载两个引擎**，差异收敛为**两种生命周期策略**：`ClaudeDriver` WARM（一会话一长驻进程 + stdin 跨轮喂消息）与 `CodexDriver` ONE-SHOT（每轮新进程 + `exec resume <thread_id>`）。双引擎均实测通过（claude 2026-08-12 / codex 2026-08-26，后者在内部 litellm 网关 + Responses API 环境）。与 [[0028_desktop-claude-transport-architecture]] 组成「外部 agent 接入协议」双页：本页是 **xihe 内核侧**的协议对照与驱动架构，0028 是**桌面端** claude 传输层细节。

## 核心要点

- **接入点 = 本地 CLI 子进程**（IPC，非 MCP——决策理由见下「已决」节）。凭据经 env 注入（`ANTHROPIC_API_KEY`/`CODEX_API_KEY`），**永不进 argv**；日志走 sanitize_error。
- **协议同构是适配层成立的前提**：NDJSON stdout 事件流 + 首事件携带会话 id（`session_id`/`thread_id`）+ `--resume` 冷续 + cwd 即 workspace + 无协议级中断（统一杀进程树）。
- **唯一结构性差异 = 生命周期**：claude 可长驻多轮（stdin 保持打开），codex `exec` 一次性（跑到 `turn.completed` 进程即退）。adapter 据此拆成两个 driver 策略，共享 spawn/清扫/中断机制层。
- **会话 id 必须按 `(engine, session_key)` 双键存**——同一 xihe 会话交替用两引擎时防串线（claude 的 sid 喂给 codex 的 resume 是未定义行为）。
- **stdin 语义两引擎方向相反**（各自最大的坑）：claude 靠 stdin 首行触发 boot 且 stdin 长驻（[[0029]] stdin-boot 死锁）；codex 把 stdin 当 prompt 源、**EOF 才开跑**——写完必须立即 close。

## 双引擎驱动架构（src/core/external_agent.py）

```
ExternalAgentDriver（接口）
└─ _SweptDriver（首跑触发孤儿清扫）
   ├─ ClaudeDriver  —— WARM：一会话一长驻进程，stdin 跨轮喂 NDJSON user 帧；
   │                   冷路径 --resume；45s 就绪门 + 10min 空闲回收（桌面侧，见 0028）
   └─ CodexDriver   —— ONE-SHOT：每轮 spawn，prompt 写 stdin 后立即 close（EOF=boot 门）；
                       轮末 kill_tree + 摘 PID，不留跨轮进程；无 idle/ready 状态机
```

**共享机制层**（两 driver 共用，新增引擎近似套模板）：
- `_resolve_bin` + `_routed_argv`（Windows `.cmd`/`.bat` → `cmd.exe /c` 路由，避 WinError 193）+ `_spawn_cli`（piped stdio + posix `start_new_session` / nt `CREATE_NEW_PROCESS_GROUP`）+ `_start_stderr_drain`（daemon 线程，stderr 不堵管道）。
- 中断 = `_TreeKillHandle` 注册进子进程注册表（复用 [[0016]] 机制），杀进程树。
- PID 文件带 `engine` 字段；`sweep_orphans()` 按引擎命令行指纹（claude=`--input-format stream-json`，codex=`--disable multi_agent`）校验后清扫孤儿，防 pid 复用误杀。
- `_resume_ids: {(engine, session_key): id}` 双键 resume 表。

**CodexDriver spawn argv 契约**（`_spawn`）：
```
codex exec --json --skip-git-repo-check -C <cwd> --disable multi_agent
    [-m <model>]
    [-s <read-only|workspace-write|danger-full-access> | --dangerously-bypass-approvals-and-sandbox]
    [-c ×5  — external_agents.codex.base_url 设置时内联定义 provider "xihe"
             (model_provider / name / base_url / env_key="CODEX_API_KEY" / wire_api)]
    [extra_args…]            # external_agents.<engine>.extra_args 原样追加
                             #（claude/codex 通用，位置在各引擎 resume 旗标前）
    [resume <thread_id>]
    -                        # prompt 走 stdin
```
permission_mode：codex 原生枚举三值（**无 `bypassPermissions`**——那是 claude 的 `--permission-mode` 值）；xihe 接受 `bypassPermissions` 作跨引擎别名 → 映射到 bypass 旗标；非法值回落 `workspace-write`（默认）。env：`CODEX_API_KEY` + `PYTHONUTF8=1`。base_url 内联 provider 是**显式 opt-in**：不回退主 base_url（否则每个实例都会悄悄改写 config.toml 选好的 provider）；`wire_api` 随行，默认 `responses`（内部 litellm 网关实测），值原样透传不剥 `/v1`（codex 在其后拼 `/responses` 或 `/chat/completions`）。`extra_args` 是逃生口（如 `-c model_reasoning_effort=high`、`--ephemeral`、`--add-dir`）。

**codex 事件 → xihe 事件映射**（`_handle_line`）：

| codex 事件 | xihe 事件 |
|---|---|
| `thread.started` | 捕获 `thread_id`（存 resume 表） |
| `item agent_message`（completed） | text_delta + 累积 → complete 时 join 输出 |
| `item reasoning`（completed） | thought_delta |
| `item command_execution` / `mcp_tool_call`（started/completed） | tool_call / tool_result（status failed → "error"） |
| `item file_change`（completed，仅此一相） | tool_call + **立即补 tool_result**（无独立 result 事件，不补会留永远转圈的 trace 行） |
| `turn.completed` | complete（`"\n\n".join(texts)`） |
| `turn.failed` | error |
| 顶层 `error`（`Reconnecting...` 前缀） | 噪声，只挂起作 EOF 兜底，**不终局**；非 Reconnecting 的顶层 error 若进程随后退出 → failed |

## claude vs codex 协议对照（adapter 视角）

| 维度 | claude（[[0028]]） | codex（本页，0.146.1 实测） | adapter 处理 |
|---|---|---|---|
| headless 入口 | `claude -p` | `codex exec` | 各一份 spawn 配置 |
| 输出 | NDJSON（stream-json） | NDJSON（`--json`） | 同形 ✓ |
| 生命周期 | 长驻（一会话一进程，stdin 跨轮） | 一次性（每轮新进程） | 两个 lifecycle policy |
| 多轮续接 | 热路径 stdin + 冷 `--resume` | 只有冷 `resume <thread_id>` | codex = claude 重写前（[[0027]]）的成熟模式 |
| 会话 id | `session_id` | `thread_id` | 都从首个事件捕获；双键存储 |
| 事件 schema | `stream_event`/`result`/`system/init` | `thread.*`/`item.*` | 各一份 mapper |
| cwd/workspace | pin cwd | `-C`/`--cd`（`--add-dir` 扩 sandbox 可写） | 统一 ✓ |
| 凭据 env | `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` | `CODEX_API_KEY`（base_url 不吃 env；config 设 base_url 时经 `-c` 内联 provider，默认留 config.toml） | 各一份 env 注入 |
| 就绪信号 | `system/init`（stdin-boot 死锁） | `thread.started`（**stdin EOF 是 boot 门**——反方向的坑） | 各自硬化 |
| 中断 | 杀进程 | 杀进程 | 统一 ✓ |
| 文件改动可见性 | 无结构化事件 | `file_change` 事件（path/kind） | codex 多一亮点 |
| 平台坑 | .cmd 包装（WinError 193） | 同左 + `--disable multi_agent` 必带 + Windows sandbox 模式 | `_routed_argv` 共用；codex 两坑进 spawn 常量 |
| 项目约定文件 | CLAUDE.md | **AGENTS.md**（`_CONV_FILE` 按引擎） | 工具层约定注入指令分引擎 |

## codex headless 协议要点（0.146.1 实测校订）

- **入口**：`codex exec [PROMPT]`。prompt 走 argv 或 stdin（`-` 占位）；**prompt 在 argv 且 stdin 是管道时，codex 仍阻塞等 stdin EOF**（把 stdin 内容追加为 `<stdin>` 块）——driver 必须写完即 close。需 git 信任目录或 `--skip-git-repo-check`（driver 恒加）。exec 模式强制 `approval: never`（无 TTY）；`-a`/`--search` 是全局 flag（须在 `exec` 前）。
- **输出**：`--json` → stdout JSONL，每行 `{type:...}`。`item.id` 稳定，started/updated/completed 复用同一 id（updated 忽略）。成功信号 = `turn.completed`；失败 = `turn.failed`；exit 0 成功 / 1 失败。item 级事件族：`agent_message`（仅 completed）/ `reasoning` / `command_execution`（`aggregated_output` 截断 64KiB）/ `file_change` / `mcp_tool_call` / `web_search` / `todo_list`。
- **续接**：`codex exec [flags] resume <thread_id> -`——新进程 + resume，thread_id 复用、token 累积、`cached_input_tokens` 命中。`resume --last` 亦可。
- **凭据优先级**（实测）：`CODEX_API_KEY` > `~/.codex/auth.json` > `OPENAI_API_KEY`（**故意被降级，别用**）。`CODEX_HOME` 重定向数据根（≈ xihe 的 `AGENT_HOME`）。base_url **不吃 env**（`OPENAI_BASE_URL` 只作用于默认 openai provider，还会 append `/responses`）；但有第三条配置通道：`exec -c key=value` 覆盖（value 按 TOML 解析，失败回落原始字符串），`model_provider` / `model_providers.<id>.base_url|env_key|wire_api` 均可覆盖——**实测纯 `-c` 定义一个 config.toml 里不存在的 provider 全链路可用**（0.146.1，内部网关）。xihe 默认留 config.toml；config 设 `external_agents.codex.base_url` 时 driver 内联定义 provider `xihe`（`wire_api` 默认 responses、不回退主 base_url——避免悄悄改写 config.toml 的 provider 选择）。相关旗标：`--oss` / `--local-provider <lmstudio|ollama>`（仅 oss/本地场景）、`-p/--profile`（叠加 `$CODEX_HOME/<name>.config.toml`）、`--ignore-user-config`（不加载 config.toml）、`--ephemeral`（不落会话文件）。
- **sandbox/权限**：`-s/--sandbox` 枚举仅 `read-only | workspace-write | danger-full-access` 三值；bypass 等价物是独立旗标 `--dangerously-bypass-approvals-and-sandbox`。
- **MCP**：codex 自身是 MCP client（`config.toml [mcp_servers.x]`，改配置要重启）；`mcp_tool_call` 事件可在 JSONL 观察。
- **app-server**：`codex app-server`（JSON-RPC 2.0 双向，类 MCP）是潜在的「长驻双向」方案；当前 one-shot 每轮 ~2-3s 启动开销实测可接受（一轮总 16s，大头在网关往返），**无痛点不动**。

## 平台硬坑（实测定位，spawn 常量已固化）

1. **`--disable multi_agent` 必带。** codex 0.146 默认开 `[features] multi_agent`，请求带 `{"type":"namespace","name":"multi_agent_v1"}` 工具 → litellm 网关 500 "Unsupported tool type: namespace"；而 codex 把所有可重试错误包成 **"We're currently experiencing high demand"** 重试 5 次后 `turn.failed`——报错完全误导（非负载问题）。定位方式：mitm 抓包 + 原样重放。修复：spawn 恒加 `--disable multi_agent`（或 config.toml `[features] multi_agent=false`）。
2. **Windows sandbox 用 `unelevated`。** `[windows] sandbox` 只认 `elevated|unelevated`（无 `disabled`）；`elevated` 走 CreateProcessWithLogonW，需 sandbox 账号有 SeBatchLogonRight——未授予则报 1385，**所有 shell 命令失败**。`unelevated`（当前用户 + 受限 token）正常。sandbox 用户密码存 `~/.codex/.sandbox-secrets/`（DPAPI），展示须掩码。

## 已决：接入模式 = IPC（2026-08-12）

**走 IPC（原生 headless 子进程驱动），不走 MCP**：

1. **语义契合（决定性）**：agent 长时/有状态/多步/流式/可中断，MCP 的 tool-call 请求-响应语义与之根本错配（阻塞/超时/无流式/无中断）；IPC 原生 NDJSON 直连 1:1 契合，事件一次映射不丢粒度。
2. **产品语义是「借脑」**：会话级 delegate（把子任务整个托付），非单步工具。
3. **复用**：ClaudeRunner（桌面）已实测；codex 同构，adapter 抽象后新增引擎近似套模板——2026-08-26 落地 CodexDriver 印证（套模板 + 两个平台坑）。
4. **进程开销不构成选 MCP 的理由**：开销来自 agent 本身，换 MCP 省不掉；控进程靠生命周期管理（WARM 空闲回收 / ONE-SHOT 轮末即清）。

**混合暴露**：传输层 IPC 做重活；触发层可薄封装成 MCP tool 给 xihe 自己的 agent 在 tool-calling 里选——两者不矛盾。MCP 的标准化价值留给「对外提供能力」的未来场景。

## 开放点

- **能力共享边界**：外部引擎当前只有自己的 file/shell 面，能否/如何用 xihe 的 skill/MCP/内部工具（IPC 模式下 prompt 注入或回调；MCP 模式靠 reciprocal）——待需求出现再定。
- app-server 长驻优化：挂起，无痛点不动。

## 落地与验证

- 代码：`src/core/external_agent.py`（机制层 + 双 driver）、`src/tools/external_agent_tool.py`（`_ENGINES` 对称：bin 检查/凭证/permission_mode 默认/约定文件 AGENTS.md）、`src/core/prompts.py`（EXTERNAL_AGENT_GUIDANCE 双引擎触发词）。config：`external_agents.claude|codex`（见 `config.example.yaml`；codex 另有 `base_url`（内联 provider，opt-in）/`wire_api`；`extra_args` 两引擎通用，原样追加在 resume 旗标前）。
- 测试：`tests/test_external_agent_codex.py` 14 项 hermetic（argv 契约/stdin 写后即关/凭据走 env 不走 argv/permission_mode 映射/全事件映射/turn.failed/Reconnecting 噪声/resume 双键隔离/中断注册与轮末清理/单例/**base_url 内联 provider 五连 -c + opt-in 不回退主 base_url**/**extra_args 位于 resume 前**）+ claude 侧 extra_args 位次测试（`test_external_agent.py`）；全量 pytest 348 绿。
- E2E（2026-08-26，真实网关）：两轮——T1 completed 16.09s 含 shell 执行；T2 `resume` 记忆成功（答出 T1 命令内容）。另：内联 provider E2E（base_url 只在 spec、config.toml 的 zp 未动）→ `-c` 覆盖生效；`extra_args=['--ephemeral']` 旗标位真实可用。

## 相关页面

- [[0028_desktop-claude-transport-architecture]] — claude 侧基线（桌面端传输层），与本页组「外部 agent 接入协议」双页
- [[0029_desktop-dual-engine-architecture]] — 桌面端双引擎总览 hub（含 ClaudeRunner 生命周期硬化 5 项）
- [[0027_desktop-claude-longlived-rewrite]] — claude 长驻重写；其「重写前」逻辑 = codex 的接入模式
- [[0026_desktop-agent-model-built-in-xihe]] — Agent = 类型（内置 xihe + 可添加 claude）；本页是「可添加」的传输层细化
- [[0016_interrupt-stop-steer]] — 子进程注册表 + 杀进程树中断机制（driver 复用）
- [[0005_mcp-dynamic-registration]] — xihe 的 MCP client 机制（对照：为何不走 MCP 接入）

## 来源

- **本机实测**：codex 0.146.1 + 内部网关（litellm，Responses API 直连可用），2026-08-26；mitm 抓包复现定位 multi_agent namespace 500；`codex exec --help` 核实 sandbox 枚举与旗标；E2E 两轮验证 resume。
- [Codex exec --json event cheatsheet — takopi](https://takopi.dev/reference/runners/codex/exec-json-cheatsheet/)（完整事件类型清单）
- [Codex CLI exec mode experiments — alexfazio gist](https://gist.github.com/alexfazio/359c17d84cb6a5af12bac88fa1db9770)（codex 0.114.0 实测）
- [Non-interactive mode — ChatGPT Learn（官方）](https://learn.chatgpt.com/docs/non-interactive-mode)（`--json` JSONL 官方说明）
- [Codex CLI config/mod.rs — Fossies 源码](https://fossies.org/linux/codex-rust/codex-rs/core/src/config/mod.rs)（exec 强制 `Never` approval）
