---
type: change
title: 会话管理命令——列出/查看/恢复/切换历史会话
slug: 0020_session-management-commands
change_type: feature
risk_level: low
status: completed
created: 2026-07-30
updated: 2026-07-30
affected_modules:
  - core/session.py
  - gateway/commands.py
  - cli/chat.py
  - cli/app.py
related_concepts:
  - wiki/concepts/0006_session-design.md
  - wiki/concepts/0011_gateway-architecture.md
---

# 会话管理命令——列出/查看/恢复/切换历史会话

## 摘要

一组面向用户的会话管理能力:列出历史会话、查看当前会话信息与历史、恢复/切换到某段历史对话。**复用已有会话模型**(见 [[0006_session-design]])——"恢复"不是新机制,就是复用同一个 `session_key` → `agent.chat()` 的 `load_messages` 读回整段历史。命令 CLI/gateway 共用,个别 CLI-only / gateway-only。

## 背景:会话模型(详见 [[0006]])

- 两层 ID:`session_key`(逻辑、确定性、从 `SessionSource` 派生)+ `session_id`(物理、重置后变)。消息按 `session_id` 存。
- **任何 agent 运行**(用户对话 / cron / delegate 子 agent)都是 session,统一进 `sessions` 表。
- 重置策略默认 `idle`(24h 无活动清空);`session.default_reset: none` 可永久保留。
- **与 Claude Code 对比**:Claude 每会话一个 JSONL 文件、随机 UUID、按 cwd 分目录、resume 时**重生成**系统提示词;xihe 用 SQLite、**确定性 key**(从 chat_id 重算)、key 内含 platform+chat_id、resume 用**缓存的**系统提示词。

## 新增能力

### 后端:`SessionDB.list_sessions(limit, platform, user_id, include_internal)`
查 `sessions` + 聚合 `messages` 得 `msg_count`,按 `updated_at` 倒序;可选 `platform` / `user_id` 过滤;默认 `include_internal=False` 排除 cron/delegate。`if user_id:` 守卫避开 SQL `WHERE user_id = NULL`(永不匹配)的坑。

### 斜杠命令(`gateway/commands.py`,CLI + gateway 共用)
- `/sessions` — 列历史会话(最近优先,标题/时间/消息数)。**gateway 按当前 user 过滤**(只列自己的,堵隐私);**默认隐藏 cron/delegate**(内部转录)。
- `/history [N]` — 当前会话历史,N=条数(默认 **20**,原 50)。条数可控,解决 gateway 单条消息长度截断。
- `/status` — 当前会话信息(key/标题/平台/模型/上下文/工具数/调度器)。
- `/resume [<n|name>]` — **会话内切换**(CLI only):选号或直选 → 切到那段历史继续。gateway 拒绝(会话绑死 chat_id)。

### CLI 入口(`cli/`)
- `xihe chat -r` / `--resume` — **启动时**交互选号恢复(REPL 前)。
- `/resume` — **REPL 内**切换(上)。
- `xihe chat -s <名>` — 命名 / 直接恢复(已有)。
- 裸 `xihe chat` — **每次启动新会话**(fresh:唯一 chat_id `auto_{ts}`,不带历史/旧提示词);要续接用 `-r` 或 `-s`。(注:原默认恢复 `cli_default`,按"每次启动全新"的诉求改为 fresh-by-default——这样换目录/换配置不会带旧上下文。)

### 跨会话搜内容
问 agent("之前聊过 X 吗")→ `session_search` 工具(FTS5,已有)。

## 接入点(改动文件)

| 文件 | 改动 |
|---|---|
| `core/session.py` | 新增 `list_sessions(limit, platform, user_id, include_internal)` |
| `gateway/commands.py` | `/sessions`(user 过滤+隐藏内部)、`/history [N]`、`/resume`、`/help` 同步 |
| `cli/app.py` | `chat` 子命令加 `-r` / `--resume` 参数(含默认 chat 的 `args.resume=False` 兜底) |
| `cli/chat.py` | `_pick_session`(启动选号)+ `run_chat` resume 逻辑;REPL 改读 `cmd_ctx["cli_source"]`(支持会话内切换) |

## 设计决策

- **resume = 复用 session_key,不是新机制**:`agent.chat()` 本就 `load_messages(session_id)`。`--resume` / `/resume` 只是帮你**发现 chat_id**(选号)——拿到 chat_id → `build_key` 重建 key → 续历史。vs Claude 的随机 UUID(必须持久化 UUID 或接受"本 cwd 最近")。详见 `resume 恢复逻辑`(对话记录)。
- **会话内 `/resume` 靠 `cmd_ctx` 持有可变 `cli_source`**:`run_chat` 的 REPL 循环改读 `cmd_ctx["cli_source"]`,`/resume` 改写它,下一轮 `chat()` 加载新会话——无需退出重启。
- **`/sessions` 隐藏 cron/delegate + gateway 按 user 过滤**:这些是内部转录 / 可能跨用户,不该出现在用户视角。`include_internal=True` 可取回全部(调试)。
- **`/history [N]` 默认 20**:gateway(企微)单条消息有长度上限,默认减半 + N 可控,避免截断。
- **CLI vs gateway 能力差**:gateway 会话按 chat_id 自动续、不能 `/resume`(绑死);CLI 可 `/resume` / `-r` / `-s`。

## 踩坑 / 注意

- **24h idle 自动重置**:超 24h 没动的会话,恢复时已是空的(`default_reset: none` 关掉)。
- **resume 切换后系统提示词是该会话缓存的**,非重生成——改了 `kbs.enabled` / cwd 不立即反映(需 `/reset`)。这点和 Claude(重生成)不同,是 xihe 当前相对劣势;若要"反映最新配置",可让 resume 路径重建提示词(代价:丢 prompt-cache 前缀命中)。
- **cron/delegate 共用 sessions 表**(统一会话模型)→ `/sessions` 必须 `include_internal` 过滤,否则被内部转录刷屏。
- **gateway `/clear`、`/quit` 是 CLI 哨兵**:gateway 里发会漏给 agent(不影响会话查看)。
- **横幅"Ctrl+C to exit"不准**:Ctrl+C 在提示符处只清行重提示、在任务中只中断,真正退出是 `/quit` 或 EOF(Ctrl+D / Ctrl+Z+Enter)。

## 验证

`py_compile` 全过 + 功能测:`list_sessions(user_id='本人')` 500→2(只自己的 wecom)、`include_internal` 默认隐藏 cron/delegate(300→22)、`/resume <name>` 正确切换 ctx 的 `session_key`/`cli_source` + gateway 守卫拒绝。

## 相关页面

- [[0006_session-design]] — 会话两层 ID / SessionSource / 重置策略 / system prompt 缓存(本特性的模型基础)
- [[0011_gateway-architecture]] — gateway 每消息建薄 agent + 会话按 chat_id 自动续
- [[0019_kbs-feature]] — 同期功能(kbs 子系统;共享"系统提示词 per-session 缓存"约束)
