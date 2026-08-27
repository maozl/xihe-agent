---
type: change
title: CLI 交互重写——hybrid TUI(steer + 历史补全 + 无 Enter wart)
slug: 0021_cli-hybrid-tui
change_type: feature
risk_level: medium
status: completed
created: 2026-07-31
updated: 2026-07-31
affected_modules:
  - cli/chat.py
  - core/prompts.py
  - core/agent.py
  - core/config.py
  - requirements.txt
  - pyproject.toml
related_concepts:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/changes/0020_session-management-commands.md
---

# CLI 交互重写——hybrid TUI(steer + 历史补全 + 无 Enter wart)

## 摘要

CLI 交互 REPL 从原始的阻塞 `input()` 重写为 **hybrid 模式**:空闲时用 prompt-toolkit(上箭头历史、灰色自动补全、HTML 着色提示符),处理中切到 msvcrt 非阻塞轮询(steer 注入、Ctrl+C 中断、worker 完成自动回空闲)。两者轮流占终端,不冲突 → **无 ANSI 乱码、无截断、无 Enter wart**。同时加了 cwd 注入、fresh-by-default 会话、来晚 steer 自动续跑、CODING_GUIDANCE 增强、日志 file_level 解耦、新依赖(prompt_toolkit + textual)。

## 背景

原始 CLI 是单线程阻塞 `input()` → `agent.chat()` → 循环,无法 steer(处理中打字)。尝试了多个 TUI 方案均翻车(prompt_toolkit patch_stdout ANSI 乱码;Textual 全屏无滚动条 + Windows 键盘输入不工作;full_screen Application 输入看不见)。根因:Windows 终端兼容性 + prompt_toolkit/Textual 的终端控制冲突。最终方案:**hybrid(prompt-toolkit 空闲 + msvcrt 轮询处理中)**,纯标准库,不依赖任何 TUI 框架的终端控制。

## 改动清单

### 1. hybrid REPL(`cli/chat.py` `_run_hybrid`)

**空闲(idle)**:`PromptSession` 阻塞等输入 → 上箭头历史(FileHistory)、灰色补全(AutoSuggestFromHistory)、HTML 着色提示符(`<ansicyan><b>xihe> </b></ansicyan>`)。

**处理中(turn running)**:`msvcrt.kbhit()/getwch()` 轮询(0.02s 间隔):
- 普通行 + Enter → `agent.steer()`(注入 `[用户中途补充]`)
- `/stop`/停止词 → `agent.interrupt()`(干净停:杀子进程、传子 agent)
- Ctrl+C(`\x03`)→ running 则 interrupt、idle 则退出
- `running[0]` 标志翻回 → **自动回空闲**(无 Enter wart)

**为什么没截断**:prompt-toolkit 只在空闲时占终端;处理中它不活跃 → 普通 `print()` 无冲突 → 无乱码、无截断、无消失。

### 2. worker 线程 + steer 自动续跑(`_run_turns_threaded`)

- `threading.Thread(daemon=True)` 跑 `agent.chat()`,三回调(stream_delta 行缓冲 + tool_start ⏺ + tool_call ✓)。
- **来晚的 steer 自动续跑**:worker 结束后 `_drain_steer()`,非中断 → 每条残留 steer 作为后续轮次跑(`msgs` 队列循环);中断 → 丢弃残留。
- 每条续跑的 steer 先显示 `❯ [text]`(青色) + `⏳ 处理中...`,回复紧跟其下。

### 3. cwd 注入(`core/agent.py` + `core/prompts.py` + `cli/app.py`)

- `XiheAgent.__init__(..., cwd=None)` → `self.cwd`;CLI `init_agent` 传 `Path.cwd()`。
- `build_system_prompt(..., cwd=None)` → `if cwd: parts.append("# Working Directory\n{cwd}  ({OS · shell})")`。
- 新增 `resolve_repo_path()`(`core/config.py`)。
- **per-session 缓存 + cwd 失效**:加载缓存提示词时检查 cwd 是否变化,变了就重建。
- **gateway 不注入**(cwd=None)。

### 4. fresh-by-default 会话(`cli/chat.py`)

裸 `xihe chat` → 每次新会话(唯一 `auto_{timestamp}` chat_id,空历史 + 当前 cwd 全新提示词)。`-s <名>` 恢复命名会话;`-r` 选号恢复。

### 5. CODING_GUIDANCE 增强(`core/prompts.py`)

从 5 点(偏分析)→ **7 点(分析 + 编写)**:
1. Read before write(不凭记忆)
2. Search before creating(先搜现有)
3. Minimal + verify(最小改动 + py_compile 验证)
4. Trace full chains(追溯全链路)
5. Cite evidence(file:line)
6. Security(注入/路径/密钥)
7. Plan + track(todo + delegate + 自我质疑)

修正:`todo_write` → `todo`。

### 6. 日志 file_level 解耦(`core/logging_config.py` + `cli/chat.py`)

`setup_logging(level, also_file, file_level=None)`:console 按 `level`(CLI=WARNING 安静),file 按 `file_level`(CLI=INFO 全量)。root 降到 `min(level, file_level)`。CLI 现在 agent.log 有完整 INFO 痕迹。

### 7. 工具进度打印(`core/agent.py`)

`chat()` 新增 `tool_call_start_callback(name, args_summary)`:工具**开始时**即打印 `⏺ name(args)`(之前只有完成后 `✓ name (Xs)`)。填补长工具运行期间的空白。

### 8. 新依赖

`prompt_toolkit>=3.0` + `textual>=0.8`(textual 装了但 Windows 终端不兼容,代码保留 `_run_tui` 备用)。

## 文件改动

| 文件 | 改动 |
|---|---|
| `cli/chat.py` | 全文重写:hybrid REPL + _run_turns_threaded + fresh-by-default + 多模式降级 |
| `core/prompts.py` | build_system_prompt(cwd, kbs_preamble) + load_kbs_preamble() + _platform_hint() + CODING_GUIDANCE 7 点 |
| `core/agent.py` | cwd 属性 + cwd 缓存失效 + tool_call_start_callback + steer 自动续跑在 _run_turns_threaded |
| `core/config.py` | resolve_repo_path() + kbs 配置段 |
| `core/logging_config.py` | file_level 解耦 |
| `cli/app.py` | -r/--resume + cwd 传递 |
| `gateway/server.py` | kbs toolset 条件追加 |
| `gateway/commands.py` | /sessions(user 过滤+隐藏内部) + /history [N] + /resume |
| `core/toolsets.py` | kbs toolset |
| `tools/kbs_tool.py` | kbs_init/status/search |
| `core/kbs_protocol.md` | 精简版协议前导 |
| `core/kbs_templates/` | 空白脚手架 |
| `config.yaml` | kbs 注释示例 |
| `requirements.txt` + `pyproject.toml` | prompt_toolkit + textual |

## 踩过的坑(给后来者)

| 方案 | 问题 | 原因 |
|---|---|---|
| prompt_toolkit + patch_stdout | `?[0m` ANSI 乱码 | patch_stdout 不解析裸 ANSI |
| prompt_toolkit + print_formatted_text(ANSI) | 未经实测就跳走 | 用户直接要求 Textual |
| Textual inline | 键盘输入不工作 | Windows 终端不兼容 Textual 输入捕获 |
| Textual full_screen | 同上 + 无原生滚动条 | alternate screen |
| Rich Console + prompt_toolkit | 用户输入消失 | Rich Console 清了 prompt_toolkit 的行 |
| prompt_toolkit HTML 提示符 | `^[[1m^[[36m` 裸 ANSI | Windows cmd 不启用 VT;改用 HTML 解决 |
| msvcrt + prompt_toolkit 各自独立 | msvcrt 无历史/补全 → "能力不够" | 合并成 hybrid(空闲 ptk + 处理中 msvcrt) |

**最终方案(hybrid)为什么能成**:prompt-toolkit 和 msvcrt **轮流**占终端(空闲 ptk 阻塞、处理中 msvcrt 轮询),从不同时 → 无终端控制冲突。

## 验证

用户终端实测通过:steer 注入(📝 已接收)、自动续跑(❯ steer → 回复)、无 Enter wart、上箭头历史、Ctrl+C 干净停、颜色正常、工具行(⏺/✓)、`/sessions`/`/resume`/`/quit`。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — 工具注册 + check_fn 门控(hybrid 的工具可见性基础)
- [[0020_session-management-commands]] — 会话管理命令(本轮扩展:fresh-by-default + `/resume` 会话内切换)
- [[0011_gateway-architecture]] — gateway steer 机制(CLI hybrid steer 的参考)
