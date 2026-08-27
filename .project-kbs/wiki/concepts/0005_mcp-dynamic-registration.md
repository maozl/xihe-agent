---
type: concept
title: MCP 动态工具注册
slug: 0005_mcp-dynamic-registration
aliases:
  - MCP
  - Model Context Protocol
tags:
  - architecture
  - tools
  - mcp
status: active
created: 2026-07-01
updated: 2026-07-01
related_pages:
  - wiki/entities/0001_xihe-agent.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
sources:
  - path: raw/sources/mcp-dynamic-registration.md
    date: 2026-07-01
---

# MCP 动态工具注册

## 摘要

xihe-agent 的 MCP 实现**不用中间代理工具**（旧实现是单一 `mcp` 工具 + `action=call`），而是把每个 MCP server 的每个工具直接注册进 `ToolRegistry`，LLM 像调用内置工具一样原生调用（工具名 `mcp_{server}_{tool}`）。常驻连接在 `SharedContext` 初始化时建立，MCP SDK 是纯 async 故跑在后台 daemon 线程的 event loop 里。

## 核心要点

- **常驻连接的必要性**: 工具 schema 必须提前注入（否则 LLM 看不到）；stdio server 每次 spawn+initialize+list_tools 耗时数秒，频繁连断浪费；已建连接调用是毫秒级、新建是秒级。加载时机是 **SharedContext 初始化**（非 import 时），CLI 与网关都触发。
- **后台 event loop**: MCP SDK 纯 async，工具调用 sync。解法——后台 daemon 线程跑 `asyncio.event_loop` 永不退出；`_run_on_mcp_loop(coro)` 把协程调度到后台 loop 阻塞等结果；每个 server 是挂在 `shutdown_event.wait()` 上的 `asyncio.Task` 保活。
- **启动流程** (`discover_mcp_tools`): 读 `config.yaml` 的 `mcp_servers` + `${ENV}` 插值 → 过滤已连接（幂等）→ 启动后台 loop → `asyncio.gather` 并行连接所有 server → 每个 server `start`(spawn→initialize→list_tools) → 对每个工具 `_convert_mcp_schema` 生成 `mcp_{server}_{tool}` → 碰撞检测（跳过与内置工具同名）→ `registry.register` + `check_fn` → `_sync_mcp_toolsets`（写 `mcp` toolset 并追加到 `full`）。
- **工具名前缀** `mcp_{server}_{tool}`: 避免跨 server 同名冲突，LLM 能从名字推断来源。`_sanitize_mcp_name` 把 `-` 转 `_`。
- **check_fn 门控**: 每个 MCP 工具注册带 `check_fn`，server 断连时该 server 所有工具自动从 LLM 可用列表隐藏。
- **Handler 闭包工厂** `_make_tool_handler(server, tool, timeout)`: 检查连接 → 构造 async `_call` → `_run_on_mcp_loop`；返回值优先 structuredContent，否则拼 text blocks；`isError=True` 提取错误文本。
- **安全**: `_build_safe_env()` 只传 PATH/HOME 等安全变量；`_sanitize_error()` 把 `ghp_`/`sk-`/`Bearer`/`token=` 替换为 `[REDACTED]`；碰撞检测；`_resolve_stdio_command()` 裸命令走 `shutil.which()`。
- **自动重连**: 5 次指数退避（1→2→4→8→16s，cap 60s），第 6 次放弃标记断连。**首次连接失败不重试**，只有成功连过的才重连。
- **配置**: `config.yaml` 的 `mcp_servers`。传输类型判断: 显式 `type: streamable-http`/`sse`/`http` 或有 `url` → HTTP；有 `command` → stdio；两者都有 → HTTP 优先打警告。字段: `command`/`args`/`env`(stdio)、`url`/`headers`(http)、`timeout`(默认120)、`connect_timeout`(默认60)。
- **当前配置示例**: `config.yaml` 配了 `企业微信文档`（streamable-http）。

## 适用场景

- 新增 MCP server: 在 `config.yaml` 的 `mcp_servers` 加配置，重启 SharedContext 即自动发现注册。
- 排查「MCP 工具不见」: 看 mcp SDK 是否装、server 是否连上（`check_fn` 门控）、是否与内置工具同名被跳过、首连是否失败（不重连）。
- 凭据安全: MCP error 会自动脱敏，但显式 env（如 `GITHUB_PERSONAL_ACCESS_TOKEN`）会原样传子进程。

## 涉及文件

`tools/mcp_tool.py`（核心）/ `core/toolsets.py`（`mcp` toolset，`full` includes `mcp`）/ `cli/app.py`（`SharedContext.__init__` 调 `discover_mcp_tools`）/ `config.yaml` / `requirements.txt`（`mcp>=1.2.0,<2` 可选）。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — MCP 工具注册进同一个 ToolRegistry
- [[0001_xihe-agent]] — SharedContext 持有 MCP 连接
- 原文快照: [raw/sources/mcp-dynamic-registration.md](../../raw/sources/mcp-dynamic-registration.md)
