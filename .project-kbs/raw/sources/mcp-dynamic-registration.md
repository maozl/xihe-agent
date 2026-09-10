# MCP 动态工具注册

## 概述

Xihe-agent 的 MCP 实现**不使用中间代理工具**，而是将 MCP server 的每个工具直接注册到 tool registry，让 LLM 像调用内置工具一样使用它们。

## 为什么需要常驻连接

1. **工具 schema 必须提前注入** — LLM 的 API 调用需要一次性传入所有可用工具的 schema。如果 MCP 工具没注册，模型看不到它们，就不会调用。
2. **连接开销大** — stdio MCP server 是子进程，每次连接要 spawn → initialize → list_tools，耗时数秒。频繁连断很浪费。
3. **调用时延** — 已建立的连接调用工具只需毫秒级，新建连接要秒级。

加载时机：**SharedContext 初始化时**（非 import 时），CLI 和网关模式都会触发。

## 架构

```
┌─────────────────────────────────────────────────┐
│  Main Thread (sync)                              │
│                                                  │
│  SharedContext.__init__()                        │
│    └─ discover_mcp_tools()                       │
│         ├─ _load_mcp_config()  (read config.yaml)│
│         ├─ _ensure_mcp_loop()  (start bg thread) │
│         └─ _run_on_mcp_loop(_discover_all())     │
│                                                  │
│  Tool handler (per tool, sync)                   │
│    └─ _make_tool_handler() → _handler()          │
│         └─ _run_on_mcp_loop(_call())             │
│              └─ session.call_tool(tool, args)    │
├─────────────────────────────────────────────────┤
│  MCP Background Thread (asyncio event loop)      │
│                                                  │
│  MCPServerTask.run()                             │
│    ├─ stdio_client(server_params)                │
│    │    └─ spawn subprocess, pipe stdin/stdout    │
│    ├─ ClientSession(read, write)                 │
│    │    └─ session.initialize()                  │
│    ├─ session.list_tools() → discover            │
│    └─ await shutdown_event  (keep-alive)         │
│                                                  │
│  Auto-reconnect: 5 retries, exponential backoff  │
└─────────────────────────────────────────────────┘
```

## 启动流程

```
SharedContext.__init__()
  │
  ├─ (其他工具初始化...)
  │
  └─ from tools.mcp_tool import discover_mcp_tools
     └─ discover_mcp_tools()
          │
          ├─ 检查 _MCP_AVAILABLE (mcp SDK 是否安装)
          │    └─ 未安装 → 返回 []，静默跳过
          │
          ├─ _load_mcp_config()
          │    └─ 从 config.yaml 读取 mcp_servers 字段
          │    └─ ${ENV_VAR} 占位符插值
          │
          ├─ 过滤已连接的 server (幂等)
          │
          ├─ _ensure_mcp_loop()
          │    └─ 创建 asyncio event loop
          │    └─ 启动 daemon 线程 "mcp-event-loop"
          │
          └─ _run_on_mcp_loop(_discover_all(), timeout=120)
               │
               └─ asyncio.gather(  ← 并行连接所有 server
                    _discover_and_register_server(name, cfg),
                    ...
                  )
                    │
                    ├─ MCPServerTask(name)
                    ├─ server.start(config)
                    │    └─ spawn stdio_client
                    │    └─ session.initialize()
                    │    └─ session.list_tools()
                    │    └─ _ready.set()
                    │
                    ├─ _register_server_tools(name, server, config)
                    │    └─ 对每个 tool:
                    │         ├─ _convert_mcp_schema() → mcp_{server}_{tool}
                    │         ├─ 碰撞检测 (跳过与内置工具同名的)
                    │         └─ registry.register(name, schema, handler, check_fn)
                    │
                    └─ _sync_mcp_toolsets()
                         ├─ TOOLSETS["mcp"]["tools"] = [...]
                         └─ TOOLSETS["full"]["tools"] += [...]
```

## 工具调用流程

LLM 看到的工具名是 `mcp_{server}_{tool}`，例如 `mcp_filesystem_read_file`。

```
LLM 发出 tool_call: mcp_filesystem_read_file({path: "/tmp/test.txt"})
  │
  └─ registry.dispatch("mcp_filesystem_read_file", args_json)
       │
       └─ _make_tool_handler 返回的 _handler(args)
            │
            ├─ 检查 server 连接状态
            │    └─ server.session is None → 返回 error
            │
            └─ _run_on_mcp_loop(_call(), timeout=120)
                 │
                 └─ session.call_tool("read_file", arguments={path: ...})
                      │
                      ├─ isError=True → 提取错误文本，sanitize 后返回
                      ├─ structuredContent 存在 → 优先返回结构化数据
                      └─ content blocks → 拼接 text 返回
```

## 关键设计决策

### 1. 工具名前缀 `mcp_{server}_{tool}`

避免不同 MCP server 的同名工具冲突，同时让 LLM 能从名字推断来源。

```python
# _convert_mcp_schema
safe_tool = _sanitize_mcp_name(mcp_tool.name)    # read-file → read_file
safe_server = _sanitize_mcp_name(server_name)     # my-server → my_server
prefixed_name = f"mcp_{safe_server}_{safe_tool}"  # mcp_my_server_read_file
```

### 2. 后台 event loop + daemon 线程

MCP SDK 是纯 async 的，但 agent 的工具调用是 sync 的。解决方式：

- **后台线程**跑一个 `asyncio.event_loop`，永不退出（daemon）
- **连接**：`_run_on_mcp_loop(coro)` 把协程调度到后台 loop，阻塞等待结果
- **长连接**：每个 server 是一个 `asyncio.Task`，在 `shutdown_event.wait()` 上挂起保持存活

### 3. Handler 闭包工厂

每个 MCP 工具的 handler 是闭包，捕获 `server_name`、`tool_name`、`tool_timeout`：

```python
def _make_tool_handler(server_name, tool_name, tool_timeout):
    def _handler(args, **kwargs) -> str:  # 符合 registry dispatch 接口
        # 1. 检查连接
        # 2. 构造 async _call() → session.call_tool()
        # 3. _run_on_mcp_loop(_call(), timeout=tool_timeout)
    return _handler
```

### 4. check_fn — 工具可用性检查

每个 MCP 工具注册时带 `check_fn`，registry 在 `get_schemas()` 时调用。如果 server 断连，该 server 的所有工具自动从 LLM 可用列表中隐藏：

```python
def _make_check_fn(server_name):
    def _check() -> bool:
        server = _servers.get(server_name)
        return server is not None and server.session is not None
    return _check
```

### 5. 安全措施

| 措施 | 说明 |
|------|------|
| 环境变量过滤 | `_build_safe_env()` — 只传 PATH/HOME 等安全变量，显式配置的 env 除外 |
| 凭证脱敏 | `_sanitize_error()` — error 信息中的 ghp_/sk-/Bearer/token= 等替换为 [REDACTED] |
| 碰撞检测 | 注册时检查是否与内置工具同名，跳过冲突 |
| 命令解析 | `_resolve_stdio_command()` — 裸命令走 `shutil.which()` 解析 |

### 6. 自动重连

连接断开后自动重试，指数退避：

```
retry 1: 1s
retry 2: 2s
retry 3: 4s
retry 4: 8s
retry 5: 16s (cap at 60s)
retry 6+: 放弃，标记 server 为断连
```

首次连接失败不重试（直接报错），只有已成功连接过的 server 才会重连。

## 配置格式

在 `config.yaml` 中：

```yaml
mcp_servers:
  # --- stdio 传输 (本地子进程) ---
  filesystem:
    type: stdio               # 可选，有 command 时自动推断
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: {}
    timeout: 120
    connect_timeout: 60

  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxxx"
    timeout: 180

  # 环境变量插值
  remote:
    command: "npx"
    args: ["-y", "some-server"]
    env:
      API_KEY: "${MY_API_KEY}"   # 从 os.environ 解析

  # --- HTTP/StreamableHTTP 传输 (远程服务) ---
  企业微信文档:
    type: streamable-http     # 推荐显式声明
    url: "https://qyapi.weixin.qq.com/mcp/robot-doc?apikey=xxx"
    headers: {}               # 自定义 HTTP 头 (可选)
    timeout: 120
    connect_timeout: 60
```

**传输类型判断：**
- 显式 `type: streamable-http` / `sse` / `http` → HTTP 传输
- 有 `url` 字段 → HTTP 传输
- 有 `command` 字段 → stdio 传输
- 两者都有 → HTTP 优先（打印警告）

**配置字段说明：**

| 字段 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 否 | 自动推断 | `stdio` / `streamable-http` / `sse` / `http` |
| `command` | string | stdio 必需 | — | MCP server 可执行命令 |
| `args` | list | 否 | [] | 命令参数 (stdio) |
| `env` | dict | 否 | {} | 传递给子进程的环境变量 (stdio) |
| `url` | string | HTTP 必需 | — | MCP server HTTP 端点 |
| `headers` | dict | 否 | {} | 自定义 HTTP 头 (HTTP) |
| `timeout` | int | 否 | 120 | 工具调用超时（秒） |
| `connect_timeout` | int | 否 | 60 | 初始连接超时（秒） |

## 公共 API

### `discover_mcp_tools() -> List[str]`

入口函数，在 `SharedContext.__init__()` 中调用。

- 读取配置 → 并行连接所有 server → 注册工具 → 同步 toolset
- 未安装 mcp SDK 时静默返回 `[]`
- 已连接的 server 不重复连接（幂等）
- 返回所有已注册的 MCP 工具名列表

### `shutdown_mcp_servers()`

关闭所有 MCP 连接，停止后台 loop。

### `get_mcp_status() -> List[dict]`

返回所有配置 server 的状态（用于 banner 显示）：

```python
[{"name": "filesystem", "transport": "stdio", "tools": 11, "connected": True},
 {"name": "github", "transport": "stdio", "tools": 0, "connected": False}]
```

## 与旧实现的区别

| 旧实现 | 新实现 |
|--------|--------|
| 单一 `mcp` 中间工具，需 `action=call` | 每个工具直接注册，LLM 原生调用 |
| 自写 JSON-RPC over stdin/stdout | 官方 `mcp` SDK |
| `MCP_ENABLED` 环境变量门控 | 有 SDK + 有配置 → 自动启用 |
| `~/.xihe-agent/mcp_servers.json` | `config.yaml` 的 `mcp_servers` 字段 |
| 无重连 | 自动重连（5次，指数退避） |
| 无凭证过滤 | error 信息凭证脱敏 + 子进程环境过滤 |
| 每次调用重新连接 | 长连接常驻，daemon 线程保活 |
| 无工具碰撞检测 | 跳过与内置工具同名的 MCP 工具 |

## 涉及文件

| 文件 | 作用 |
|------|------|
| `tools/mcp_tool.py` | MCP 核心：连接、发现、注册、调用 |
| `core/toolsets.py` | `mcp` toolset 定义，`full` includes `mcp` |
| `cli/app.py` | `SharedContext.__init__()` 调用 `discover_mcp_tools()` |
| `config.yaml` | `mcp_servers` 配置字段 |
| `requirements.txt` | `mcp>=1.2.0,<2` 可选依赖 |
