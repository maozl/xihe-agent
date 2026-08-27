---
type: concept
title: Toolset Scope 与按需工具展开
slug: 0013_toolset-scope-and-dynamic-expansion
aliases:
  - toolset scope
  - 工具裁剪
  - request_tools
  - 自动工具展开
tags:
  - architecture
  - tools
  - toolset
  - optimization
status: active
created: 2026-07-10
updated: 2026-08-16
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/entities/0001_xihe-agent.md
---

# Toolset Scope 与按需工具展开

> **⚠️ 目录部分已过时（2026-08-16 订正）**：下文「12 个 toolset 分层表」是历史形态。现行为 **14 个带中文标签的平铺组**——组合预设（debugging/safe/research/coding/full）与 `includes` 递归机制**已删除**（零消费者）、死组 `browser_scripts` 已删除、`core` 四拆为 files/terminal/dev_tool/http、`agent` 拆出 `skills`、`directory_tree` 补列。`DEFAULT_TOOLSETS = [files, terminal, dev_tool, http, memory, communication, agent, skills, ssh]`。本文的**裁剪 + request_tools 按需展开机制仍完全有效**，仅组目录以 [[0033_specialist-toolset-overhaul]] 为准。

## 摘要

xihe 有 ~65 个工具，每轮全部注入 system prompt 约 ~10-15k token。其中 browser 工具族（36 个）占了大头但大部分任务不用。通过 **toolset scope** 机制：gateway 模式默认只加载核心 toolset（~37 工具），agent 通过 `request_tools` 元工具按需展开 web/media/scheduler。省 ~4-6k token/轮。

## Toolset 分层

`core/toolsets.py` 定义了 12 个 toolset，分三层：

| 层 | toolset | 工具数 | 加载策略 |
|---|---|---|---|
| 🟢 always-on | core, memory, communication, agent, mcp | ~37 | 默认加载 |
| 🔴 on-demand | web, media, scheduler | ~41 | 需 `request_tools` 展开 |
| 组合 | debugging, safe, research, coding, full | — | 复合（includes 链） |

**DEFAULT_TOOLSETS**（gateway 默认）：`["core", "memory", "communication", "agent"]` + mcp（如有 MCP server 连接）。

### 核心工具集（core，always-on）

terminal, read_file, write_file, search_files, patch, execute_code, process, maven_dep, node_version, model_info, http, request_tools

覆盖：代码任务、文件操作、系统命令、API 调用、Maven/Node 分析、模型查询、工具展开。

### 可扩展工具集（CONDITIONAL_TOOLSETS）

```python
CONDITIONAL_TOOLSETS = {
    "web":       "browser automation (navigate/click/type), website login ( screenshots, web search",
    "media":     "image analysis (vision), OCR, text-to-speech",
    "scheduler": "cron jobs, scheduled/recurring tasks, timers",
}
```

加新可扩展 toolset = 加一行。`request_tools` 的 schema 描述也从这里动态生成。

## 按需展开机制（两层防护）

### 第一层：默认裁剪（零成本）

gateway 每条消息建 agent 时传入 `enabled_toolsets=DEFAULT_TOOLSETS + mcp`。`registry.get_schemas(toolsets=...)` 只返回指定 toolset 内的工具 → browser/media/scheduler 不进 schema → 省 token。

**不做 LLM 分类**（曾经实现过 `select_toolsets` 用 aux LLM 分类用户消息意图 → 决定加载哪些 toolset，但有问题）：
- 每条消息多一次 API 调用（~60 token + ~0.5s 延迟）
- 同步调用阻塞事件循环（曾经卡住 gateway）
- 简单任务（"几点了"）也走分类，浪费

砍掉后改为纯默认裁剪 + request_tools 兜底。

### 第二层：request_tools 元工具（agent 自主展开）

`tools/request_tools_tool.py` 注册在 core（always-on）。agent 发现需要 browser/media/scheduler 但当前 schema 里没有时，调 `request_tools(["web"])` → handler 写 `parent_agent._expansion_state` → 下一轮迭代 agent loop 重读 schema → web 工具可用。

**agent loop 每轮迭代重读 schema**（`core/agent.py`）：
```python
# 每轮迭代开头（不是 turn 开始时取一次）
if self.enabled_toolsets is not None:
    _effective_ts = set(self.enabled_toolsets) | self._expansion_state
else:
    _effective_ts = None
tool_schemas = registry.get_schemas(toolsets=_effective_ts)
```

`get_schemas` 是内存 dict 扫描（微秒级），每轮重取零成本。

### 流程示例

```
用户「分析项目依赖，顺便看下官方文档」
  → 默认 core-only（~37 工具）
  → agent 用 read_file/maven_dep 分析...
  → 发现需要浏览文档网站 → browser_navigate 不在 schema
  → agent 调 request_tools(["web"]) → "Loaded: web"
  → 下一轮迭代 → schema 含 web → browser_navigate 可用
  → agent 继续浏览 → 完成
```

_turn 结束后 `_expansion_state` 清空_（每条消息新建 agent，不累积）。

## check_fn 门控

部分工具有 `check_fn` 门控：条件不满足时**整工具从 schema 消失**（不只是标记不可用）。例如：
- browser_*：Playwright 不可导入时消失
- web_search/web_extract/web_crawl：无对应 API key 时消失（`_has_search_key` / `_has_firecrawl`）
- mcp_*：MCP server 未连接时消失
- maven_dep / node_version：mvn / nvm 不在 PATH 时消失

这跟 toolset scope 是**正交**的：toolset 决定「这组工具要不要加载」，check_fn 决定「这个工具在当前环境能不能用」。

## 配置

```yaml
session:
  toolset_scope: smart   # smart（默认，裁剪）或 all（全量，旧行为）
```

设 `all` → `enabled_toolsets=None` → 所有 check_fn 通过的工具全加载（回到改造前行为）。

CLI 模式不受影响（始终全量）。

## 设计决策与取舍

### 为什么不用向量检索式工具发现（文章「工具自主发现」方案 A）

- 50 个工具规模不够大（向量检索适合 100+ 工具）
- 召回风险：向量相似 ≠ 功能匹配，漏一个关键工具比多占 token 严重
- 额外依赖（embedding + 向量库 + 维护）

toolset scope（粗粒度分组 + agent 按需展开）是 xihe 规模下更简单可靠的替代。

### 为什么砍掉 LLM 分类

- 每条消息多一次 API 调用（延迟 + token + rate limit 压力）
- 同步调用曾阻塞事件循环导致 gateway 卡死
- request_tools 兜底已覆盖漏判场景

### request_tools 的风险

glm 不强遵循工具描述/行为规则（前例：model_info 需要加 BEHAVIOR_RULES 才被调用）。如果 agent 不主动调 request_tools，它就缺工具。缓解：BEHAVIOR_RULES 第 8 条硬引导。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — ToolRegistry + check_fn 门控（toolset scope 的基础设施）
- [[0011_gateway-architecture]] — create_agent 每消息新建 + enabled_toolsets 传递
- [[0001_xihe-agent]] — 项目总览
