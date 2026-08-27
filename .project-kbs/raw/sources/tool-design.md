# Xihe-Agent Tool Design

## Architecture Overview

```
┌─────────────────┐     dispatch(name, args, context, **kwargs)     ┌──────────┐
│  Parent Agent    │ ──────────────────────────────────────────────► │  ToolReg │
│  (XiheAgent)     │ ◄────────────────────────────────────────────── │  istry   │
│  depth=0         │              JSON string result                 └──────────┘
│  all toolsets    │                                                    │
└────────┬────────┘                                              ToolEntry
         │ parent_agent=self                                    (schema+handler)
         │
         ├──► delegate_task ──► creates child XiheAgent
         │                      ┌─────────────────┐
         │                      │  Child Agent      │
         │                      │  depth=1          │
         │                      │  restricted tools │
         │                      │  focused prompt   │
         │                      │  shared db/aux    │
         │                      └──────────────────┘
         │
         ▼
┌──────────────────┐
│  AuxiliaryClient  │  ◄── stateless single-shot LLM calls
│  (no tool loop)   │      (vision, compression, title, tts, image_gen)
└──────────────────┘
```

## Tool Registry

### ToolEntry

每个注册的工具由一个 `ToolEntry` 实例描述：

```python
class ToolEntry:
    __slots__ = (
        "name",              # 工具名称 (唯一)
        "toolset",           # 所属工具分组
        "schema",            # OpenAI function schema (不含 name 字段)
        "handler",           # 执行函数 (args, **kw) -> str
        "check_fn",          # 可用性检查函数 () -> bool
        "requires_env",      # 依赖的环境变量列表
        "is_async",          # 是否异步 handler
        "description",       # 工具描述
        "max_result_size_chars",  # 结果大小限制
    )
```

### ToolRegistry

模块级单例 `registry = ToolRegistry()`，提供：

| 方法 | 作用 |
|------|------|
| `register(name, schema, handler, ...)` | 注册工具（tool 模块 import 时调用） |
| `deregister(name)` | 注销工具（MCP 动态发现用） |
| `get_schemas(names=None, toolsets=None)` | 获取可用工具的 schema 列表（含 check_fn 过滤，支持 toolset 过滤） |
| `dispatch(name, arguments, context=None, **kwargs)` | 执行工具，返回 JSON 字符串 |
| `get_all_tool_names()` | 返回所有已注册工具名 |
| `get_toolset_for_tool(name)` | 查询工具所属 toolset |
| `is_toolset_available(toolset)` | 检查 toolset 是否可用 |
| `check_toolset_requirements()` | 返回所有 toolset 的可用状态 |

### 自注册模式

每个 tool 文件在 import 时调用 `registry.register()`，无需集中注册：

```python
# tools/terminal.py
from tools import registry, tool_error, tool_result

def _terminal(args, **kw):
    command = args.get("command", "")
    if not command:
        return tool_error("command is required")
    # ... execute command ...
    return tool_result(stdout=stdout, stderr=stderr, exit_code=code)

registry.register(
    name="terminal",
    schema={"type": "function", "function": {
        "name": "terminal",
        "description": "Execute a terminal command",
        "parameters": { ... },
    }},
    handler=lambda args, **kw: _terminal(args, **kw),
    check_fn=_check_terminal,
    toolset="core",
)
```

`load_all_tools()` 自动扫描 `tools/` 目录，import 所有 `.py` 文件触发注册。

### dispatch 调用链

```python
# core/agent.py — agent loop
registry.dispatch(tc["name"], tc["arguments"], context=_tool_context, parent_agent=self)

# tools/__init__.py — registry.dispatch
def dispatch(self, name, arguments, context=None, **kwargs):
    entry = self._tools[name]
    args = json.loads(arguments)
    kw = dict(kwargs)
    if context:
        kw["context"] = context      # chat_id, platform, session_key 等
    result = entry.handler(args, **kw)
    return result  # JSON string
```

Handler 签名统一为 `(args: dict, **kw) -> str`，通过 `**kw` 接收：
- `context`: 包含 `chat_id`, `platform`, `session_key`, `user_id`
- `parent_agent`: XiheAgent 实例引用（仅 delegate/cron 需要）

### tool_error / tool_result

消除全项目的 `json.dumps({"error": ...}, ensure_ascii=False)` 样板代码：

```python
from tools import registry, tool_error, tool_result

# Before:
return json.dumps({"error": "path is required"})
return json.dumps({"success": True, "path": str(p)}, ensure_ascii=False)

# After:
return tool_error("path is required")
return tool_result(success=True, path=str(p))
```

## Toolset 分组

| Toolset | 工具 | check_fn 作用 |
|---------|------|--------------|
| `core` | terminal, read_file, write_file, search_files, patch, execute_code, process | 检查环境是否允许执行 |
| `web` | web_search, web_extract, web_crawl, browser | 检查 API key / 环境变量 |
| `memory` | memory, session_search | 检查向量数据库配置 |
| `communication` | send_message, send_image, clarify | 检查平台适配器 |
| `media` | vision_analyze, image_generate, text_to_speech | 检查模型/API 可用性 |
| `agent` | delegate_task, todo, skills_list, skill_view | 始终可用 |
| `mcp` | mcp_* | 检查 MCP server 连接 |
| `scheduler` | cronjob | 始终可用 |

Toolset 的用途：
1. `get_schemas()` 过滤不可用工具（`check_fn` 返回 False 的 toolset 整组隐藏）
2. `get_schemas(toolsets=...)` 限制子 agent 可用工具集
3. UI 展示分组（`/tools` 命令按 toolset 显示）
4. MCP 动态工具注册/注销时批量管理

## Tool 调用模型：AuxiliaryClient

### 为什么不直接用 agent.chat()

如果 tool 内部需要 LLM 能力（如图片分析、上下文压缩、标题生成），不能调用 `agent.chat()`，原因：

| 问题 | agent.chat() | AuxiliaryClient |
|------|-------------|-----------------|
| 工具递归 | 可能触发 tool call → 无限递归 | 纯文本补全，无 tool call |
| 会话污染 | 结果写入 session history | 无状态，不写任何 history |
| 模型灵活 | 只能用当前 session model | 每个 task 可配置不同模型 |
| System prompt | 注入完整 agent prompt + tool schemas | 只用简短 task prompt |
| 超时控制 | agent 全局超时 | 每个 task 独立超时 |
| 开销 | 重（完整 agent loop） | 轻（单次 API 调用） |

### AuxiliaryClient 设计

```python
class AuxiliaryClient:
    def __init__(self, base_url, api_key, model, config):
        self._default_model = model
        self._config = config   # 包含 auxiliary 子配置
        self._client = OpenAI(...)

    def call_llm(self, task, messages, model=None, max_tokens=4000,
                 temperature=None, timeout=None) -> Optional[Response]:
        """通用文本补全 — 用于 compression, title 等"""

    def call_vision(self, messages, model=None, max_tokens=2000,
                    timeout=None) -> Optional[Response]:
        """视觉补全 — 封装 call_llm(task="vision")"""

    def generate_image(self, prompt, model=None, size="1024x1024",
                       n=1, style="vivid", timeout=None) -> Optional[Response]:
        """图片生成 — DALL-E 风格 API"""

    def text_to_speech(self, text, voice="alloy", model=None,
                       timeout=None) -> Optional[Response]:
        """语音合成 — TTS API"""

    def is_available(self, task=None) -> bool:
        """检查某个 task 是否可用"""
```

### Per-task 模型解析

每次调用通过 `_resolve(task, model, timeout)` 确定实际使用的模型和超时：

```
优先级: 显式参数 > config.yaml 配置 > 环境变量 > 默认值
```

配置示例 (`config.yaml`):

```yaml
auxiliary:
  compression:
    model: glm-4-flash        # 便宜模型做压缩
  vision:
    model: gpt-4o             # 视觉模型
    timeout: 90
  title:
    model: glm-4-flash        # 便宜模型生成标题
    timeout: 10
```

环境变量覆盖：

```bash
AUXILIARY_VISION_MODEL=gpt-4o
AUXILIARY_TITLE_MODEL=glm-4-flash
```

### 使用 Aux 的 Tool 和模块

| 模块 | Task | 用途 |
|------|------|------|
| `core/compressor.py` | `compression` | 上下文压缩摘要 |
| `core/title_generator.py` | `title` | 自动生成会话标题 |
| `tools/vision_tools.py` | `vision` | 图片分析 |
| `tools/image_generation_tool.py` | `image_gen` | 图片生成 |
| `tools/tts_tool.py` | `tts` | 语音合成 |

初始化方式（`cli/app.py`）：

```python
agent = XiheAgent(config)

# Aux 已在 XiheAgent.__init__ 中创建
vision_tools.set_auxiliary(agent.aux)
image_generation_tool.set_auxiliary(agent.aux)
tts_tool.set_auxiliary(agent.aux)
```

## Tool 获取 Agent 引用

少数 tool 需要调用 `agent.chat()` 创建子会话（delegate、cron），通过 `parent_agent` kwargs 注入：

### cronjob_tools

调度器在后台线程运行，第一次 dispatch 时通过 kwargs 获取 agent 引用并缓存：

```python
_agent = None

def _inject_agent(agent):
    global _agent
    if agent and not _agent:
        _agent = agent

def _cronjob(args, **kw):
    _inject_agent(kw.get("parent_agent"))
    # ... handle action ...
```

### 不使用全局变量的原因

1. **测试友好** — 不依赖模块级状态，可以注入 mock
2. **多实例安全** — 如果未来有多 agent 实例，不会串扰
3. **依赖可见** — 从 `registry.dispatch()` 调用链就能看出 agent→tool 的依赖关系

## 子 Agent (Subagent) 设计

delegate_task 不直接调用 parent 的 `agent.chat()`，而是创建**独立的子 XiheAgent 实例**，实现隔离执行。

### 为什么需要子 Agent

| 问题 | 直接用 parent.chat() | 子 XiheAgent 实例 |
|------|----------------------|-------------------|
| 上下文隔离 | 子任务混入 parent session history | 子 agent 拥有独立 session |
| 工具限制 | 子任务可用全部工具（含危险工具） | 受限 toolset，blocked toolsets 被剥离 |
| 递归防护 | 无，可能无限 delegate | 深度限制 MAX_DEPTH=2 |
| System prompt | 继承 parent 完整 prompt（含 SOUL.md + memory） | 任务专属聚焦 prompt |
| 自动标题 | 子任务也会触发 auto-title | 子 agent 跳过 auto-title |
| 批量并行 | 不支持 | ThreadPoolExecutor 并行执行 |

### 完整交互流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ Parent Agent (depth=0)                                              │
│                                                                     │
│  1. LLM 决定调用 delegate_task(goal=..., context=...)               │
│  2. registry.dispatch("delegate_task", args, parent_agent=self)     │
│     │                                                               │
│     ├── 深度检查: depth < MAX_DEPTH (2)                             │
│     ├── 构建 child XiheAgent (受限 toolset, 聚焦 prompt)            │
│     ├── 注册 child 到 parent._active_children (中断传播)            │
│     │                                                               │
│     ├── 单任务: 直接调用 child.chat() (同步, 当前线程)              │
│     │   批量:   ThreadPoolExecutor 并行调用 child.chat()             │
│     │                                                               │
│     ├── child.chat() 内部:                                          │
│     │   ├── agent loop (max_iterations=30)                          │
│     │   ├── 每次 API 调用有 120s 超时保护                           │
│     │   ├── 工具结果经三层防御 (persist + budget)                   │
│     │   ├── context 压缩在超阈值时自动触发                          │
│     │   └── 返回最终响应或错误                                      │
│     │                                                               │
│     ├── 从 session history 提取 tool_trace                          │
│     ├── 注销 child 从 _active_children                              │
│     └── 返回结构化结果 {status, summary, tool_trace, ...}           │
│                                                                     │
│  3. LLM 收到 delegate_task 结果, 继续对话                           │
└─────────────────────────────────────────────────────────────────────┘
```

### 防卡死机制（无 thread-based timeout）

**不使用 `threading.Thread` + `thread.join(timeout)` 强制超时**。原因：

| 方案 | 问题 |
|------|------|
| `thread.join(timeout)` | 超时后线程仍在运行，无法真正停止；daemon 线程在 gateway 长驻进程中持续消耗资源 |
| `child.interrupt()` | 只设置标志位，无法中断正在进行的 HTTP 请求 |
| `os.kill()` | 过于粗暴，可能破坏共享状态（SessionDB、AuxiliaryClient） |

Xihe 的三层防卡死设计：

```
┌──────────────────────────────────────────────┐
│ Layer 1: max_iterations (默认 30)            │  ← 限制 agent loop 迭代次数
│ Layer 2: API call timeout (120s per call)    │  ← 防止 HTTP 请求无限挂起
│ Layer 3: Parent interrupt propagation        │  ← 用户发新消息时中断子 agent
└──────────────────────────────────────────────┘
```

- **Layer 1**: `child.max_iterations` 限制 agent loop 最多跑 30 轮（可配置）
- **Layer 2**: `_call_with_retry()` 内用 `ThreadPoolExecutor` + `future.result(timeout=120)` 给每次 API 调用加超时。超时后抛 `TimeoutError`，agent 返回 `"API timeout: ..."`
- **Layer 3**: 子 agent 注册到 `parent._active_children`。当用户发新消息时，gateway 调用 `parent.interrupt()` → 遍历 `_active_children` → `child.interrupt()` → 设置 `_interrupt_requested=True` → agent loop 在下次迭代时退出

### 中断传播

```python
# gateway/server.py — 用户发新消息时
with _active_agents_lock:
    active = _active_agents.get(session_key)
if active:
    active.interrupt()  # → 遍历 active._active_children → child.interrupt()

# core/agent.py — interrupt() 实现
def interrupt(self):
    self._interrupt_requested = True
    from tools.interrupt import set_interrupt
    set_interrupt(True)
    with self._active_children_lock:
        for child in self._active_children:
            child.interrupt()  # 递归传播到所有子 agent
```

子 agent 在 `_run_single_child` 的 `finally` 块中自动从 `_active_children` 注销，确保不会留下悬挂引用。

### XiheAgent 子 Agent 参数

`XiheAgent.__init__` 新增 3 个 keyword-only 参数，控制子 agent 行为：

```python
class XiheAgent:
    def __init__(self, config=None, *,
                 enabled_toolsets: list[str] | None = None,
                 delegate_depth: int = 0,
                 system_prompt_override: str | None = None):
```

| 参数 | 作用 | Parent 值 | Child 值 |
|------|------|-----------|----------|
| `enabled_toolsets` | 限制可用工具集 | `None`（全部可用） | blocked 剥离后的 toolset 集合 |
| `delegate_depth` | 委托深度 | `0` | `parent.delegate_depth + 1` |
| `system_prompt_override` | 覆盖系统提示 | `None`（用 SOUL.md + memory） | 任务聚焦 prompt |

`chat()` 中的行为差异：
- **工具 schema**: `registry.get_schemas(toolsets=self.enabled_toolsets)` — 只返回允许的 toolset
- **系统提示**: `self.system_prompt_override or self._build_system_prompt()` — 有覆盖则用覆盖
- **Auto-title**: `delegate_depth == 0` 时才生成标题
- **Memory 注入**: `delegate_depth > 0` 时跳过 memory context

### Blocked Toolsets

子 agent 永远不能使用的工具分组：

```python
DELEGATE_BLOCKED_TOOLSETS = frozenset([
    "agent",          # 不能递归 delegate/todo/skills
    "communication",  # 不能向用户发消息/clarify
    "scheduler",      # 不能操作 cron
])
```

toolset 解析逻辑（`_resolve_allowed_toolsets`）：

```
child_toolsets = (requested ∩ parent_toolsets) - BLOCKED_TOOLSETS
```

1. 如果指定了 `toolsets` 参数：与 parent 的 toolsets 取交集（子不能获得 parent 没有的工具）
2. 如果未指定：继承 parent 的全部 toolsets
3. 最后剥离 blocked toolsets
4. 如果结果为空：回退到 DEFAULT_TOOLSETS (`core`, `web`, `memory`, `media`)

### 子 Agent 的创建与资源共享

```python
child = XiheAgent(
    config=parent.config,               # 共享配置
    enabled_toolsets=allowed_toolsets,   # 受限 toolset
    delegate_depth=parent.delegate_depth + 1,
    system_prompt_override=child_prompt, # 聚焦 prompt
)

# 共享资源（避免重复创建）
child.db = parent.db            # 同一个 SessionDB
child.aux = parent.aux          # 同一个 AuxiliaryClient
child.compressor = parent.compressor  # 同一个 ContextCompressor
```

子 agent 独立拥有的：`client`(OpenAI)、`session`、`tool_schemas`、`max_iterations`

### 深度限制

```
MAX_DEPTH = 2

parent (depth=0) → child (depth=1) → ✗ grandchild (depth=2 被拒绝)
```

当 `agent.delegate_depth >= MAX_DEPTH` 时，`delegate_task` 直接返回错误。

### 子 Agent 系统提示

`_build_child_system_prompt(goal, context, workspace_path)` 构建聚焦 prompt：

```
You are a focused subagent working on a specific delegated task.

YOUR TASK:
{goal}

CONTEXT:
{context}

WORKSPACE PATH:
{workspace_path}

Complete this task using the tools available to you.
When finished, provide a clear, concise summary of:
- What you did
- What you found or accomplished
- Any files you created or modified
- Any issues encountered
```

不加载 SOUL.md、不加载 memory、不加载 project context — 子 agent 只关注被委托的任务。

### 执行模式

**单任务模式**：提供 `goal`，直接在当前线程运行（无 ThreadPool 开销）

**批量并行模式**：提供 `tasks` 数组（最多 3 项），ThreadPoolExecutor 并行执行

```python
# 单任务
delegate_task(goal="debug the auth flow", context="src/auth.py returns 403")

# 批量并行
delegate_task(tasks=[
    {"goal": "research API rate limits", "toolsets": ["web"]},
    {"goal": "write unit tests for auth", "toolsets": ["core"]},
])
```

### 结构化结果

返回 JSON 格式，非原始字符串：

```json
{
  "results": [
    {
      "task_index": 0,
      "status": "completed",
      "summary": "Found the bug: token expiry check was using wrong timestamp...",
      "duration_seconds": 3.2,
      "exit_reason": "completed",
      "tool_trace": [
        {"tool": "directory_tree", "args_bytes": 45, "result_bytes": 1200, "status": "ok"},
        {"tool": "read_file", "args_bytes": 30, "result_bytes": 8500, "status": "ok"},
        {"tool": "search_files", "args_bytes": 25, "result_bytes": 3200, "status": "ok"}
      ]
    },
    {
      "task_index": 1,
      "status": "max_iterations",
      "summary": "Reached maximum iterations.",
      "duration_seconds": 15.1,
      "exit_reason": "max_iterations",
      "tool_trace": [...]
    }
  ],
  "total_duration_seconds": 15.3
}
```

| status | 含义 |
|--------|------|
| `completed` | 子 agent 正常完成，产出了有效响应 |
| `max_iterations` | 子 agent 达到最大迭代次数 |
| `interrupted` | 子 agent 被用户中断 |
| `api_timeout` | API 调用超时（120s） |
| `api_error` | API 调用失败（非超时） |
| `failed` | 子 agent 没有产出有效响应 |
| `error` | 子 agent 运行时抛出异常 |

### tool_trace 诊断

每个结果包含 `tool_trace` 数组，记录子 agent 的完整工具调用链：

```json
{
  "tool": "read_file",
  "args_bytes": 42,
  "result_bytes": 12500,
  "status": "ok"
}
```

- `tool`: 工具名称
- `args_bytes`: 调用参数大小（字节）
- `result_bytes`: 返回结果大小（字节）
- `status`: `"ok"` 或 `"error"`

当子 agent 失败/超时时，`tool_trace` 帮助定位卡在哪一步（如某个 read_file 返回了超大结果导致 context 溢出）。

### 委托模型配置

子 agent 可以使用不同的模型（省钱或用更强的推理能力）：

```yaml
# config.yaml
delegation:
  model: glm-4-flash      # 子 agent 用更便宜的模型
  max_iterations: 30      # 子 agent 最大迭代次数
```

解析优先级：`toolsets` 参数中的 model > `delegation.model` 配置 > parent 的 model

## 上下文溢出防御（Three-Layer Defense）

当工具返回大量数据时（如 read_file 读取大文件、web_crawl 抓取整站），上下文可能膨胀到超过模型窗口，导致 API 调用挂起或报错。三层防御逐层拦截：

### Layer 1: Per-Tool Output Cap

各工具自行截断输出。这是已有的行为——每个 tool handler 在返回前检查结果大小并截断。

示例：`terminal` 工具限制 stdout/stderr 长度，`search_files` 限制匹配行数。

### Layer 2: Per-Result Persistence

**触发条件**：单个工具结果超过 `DEFAULT_RESULT_SIZE`（30,000 字符）

**机制**：将完整内容写入临时文件，上下文中只保留预览 + 文件路径。LLM 可通过 `read_file` 按需读取完整内容。

**实现**：`tools/tool_result_storage.py` → `maybe_persist_tool_result()`

```
Agent loop 中每次 tool dispatch 后：
  result = registry.dispatch(tc["name"], tc["arguments"], ...)
  result = maybe_persist_tool_result(content=result, tool_name=tc["name"], tool_use_id=tc["id"])
  # result 可能被替换为 <persisted-output> 预览块
```

**LLM 看到的替换格式**：

```
<persisted-output>
This tool result was too large (125,432 characters, 122.5 KB).
Full output saved to: /tmp/xihe-agent-results/call_abc123.txt
Use the read_file tool with offset and limit to access specific sections.

Preview (first 1500 chars):
...原始内容的前 1500 字符...
...
</persisted-output>
```

**Pinned 工具**：`read_file` 永远不会被持久化到文件，防止无限循环（read_file → persist → read_file → persist ...）。对 pinned 工具，超过阈值时做内联截断：

```
...前 30,000 字符...

[Truncated: tool response was 125,432 chars. Use read_file with offset/limit to read specific sections.]
```

### Layer 3: Per-Turn Aggregate Budget

**触发条件**：单轮所有工具结果的总字符数超过 `DEFAULT_TURN_BUDGET`（150,000 字符）

**机制**：按结果大小降序排列，将最大的结果逐个持久化到文件，直到总大小低于预算。

**实现**：`tools/tool_result_storage.py` → `enforce_turn_budget()`

```
Agent loop 中每轮所有 tool call 完成后：
  enforce_turn_budget(messages[-num_tool_msgs:])
  # 最大的结果可能被持久化以释放空间
```

### 常量与调优

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_RESULT_SIZE` | 30,000 | Layer 2 触发阈值（字符数） |
| `DEFAULT_TURN_BUDGET` | 150,000 | Layer 3 总预算（字符数） |
| `DEFAULT_PREVIEW_SIZE` | 1,500 | 持久化后保留的预览长度 |
| `PINNED_INF` | `{"read_file"}` | 永不持久化的工具集合 |

调优建议：
- 128K 上下文模型：默认值通常足够
- 32K 上下文模型：建议 `DEFAULT_TURN_BUDGET` 降到 60,000，`DEFAULT_RESULT_SIZE` 降到 15,000
- 如果经常触发 Layer 3 且 LLM 能正常处理：可适当提高 `DEFAULT_TURN_BUDGET`

### 存储位置

持久化文件写入 `%TEMP%/xihe-agent-results/` 目录，文件名格式 `{tool_use_id}.txt`：

```
/tmp/xihe-agent-results/
├── call_abc123.txt     # Layer 2 持久化
├── call_def456.txt     # Layer 2 持久化
└── budget_2.txt        # Layer 3 预算溢出持久化
```

### Agent Loop 集成

```python
# core/agent.py — 完整调用链

# 每个工具调用后 (Layer 2)
result = registry.dispatch(tc["name"], tc["arguments"], context=_tool_context, parent_agent=self)
result = maybe_persist_tool_result(content=result, tool_name=tc["name"], tool_use_id=tc["id"])

# 所有工具结果追加后 (Layer 3)
enforce_turn_budget(messages[-num_tool_msgs:])
```

Layer 2 对每个结果独立触发，Layer 3 对整轮结果统算。即使单个结果不大（未触发 Layer 2），多个结果累加仍可能触发 Layer 3。

## Import Chain (避免循环引用)

```
tools/__init__.py    (定义 registry, tool_error, tool_result — 不 import agent)
       ▲
tools/*.py           (from tools import registry, tool_error, tool_result)
       ▲
core/agent.py        (from tools import registry, load_all_tools)
```

- `tools/__init__.py` 不 import 任何 agent/core 模块
- 各 tool 文件只 import `tools` 包内的符号
- `core/agent.py` import `tools` 并在 `__init__` 后调用 `load_all_tools()`
- `AuxiliaryClient` 在 `core/auxiliary_client.py` 中独立定义，tool 文件通过 `set_auxiliary()` 注入
