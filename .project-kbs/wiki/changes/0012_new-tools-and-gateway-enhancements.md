---
type: change
title: 新增 http/maven/node 工具 + web 工具门控 + gateway/file/mcp 增强
slug: 0012_new-tools-and-gateway-enhancements
change_type: feature
risk_level: low
status: completed
created: 2026-07-09
updated: 2026-07-09
affected_modules:
  - tools/http_tool.py (新增)
  - tools/maven_tool.py (新增)
  - tools/node_version_tool.py (新增)
  - tools/web_tools.py
  - core/toolsets.py
  - gateway/server.py
  - tools/file_tools.py
  - tools/mcp_tool.py
  - tools/cronjob_tools.py
  - core/prompts.py
  - platforms/wecom.py
  - config.yaml
related_concepts:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
---

# 新增 http/maven/node 工具 + web 工具门控 + gateway/file/mcp 增强

## 摘要

两组改动：(1) 新增 3 个开发者工具（http/maven_dep/node_version）+ web 工具加 check_fn 门控按需隐藏；(2) gateway、文件工具、MCP 工具、cron 工具、企微适配器的增强修复。

## 变更一：新增 3 个工具（`tools/http_tool.py`、`tools/maven_tool.py`、`tools/node_version_tool.py`）

### http 工具
- 通用 HTTP/REST API 调用工具，支持 GET/POST/PUT/PATCH/DELETE 等方法。
- JSON body/params 原生 dict，JSON 响应自动解析。替代手写 urllib/curl。
- 支持内部 IP（用于调用内部服务）、自定义 headers、SSL 控制。
- 注册到 `web` toolset，read_only=True。

### maven_dep 工具
- Maven 项目依赖分析，运行 mvn 命令。
- 5 个 action：tree（依赖树+冲突）、conflicts（版本冲突）、analyze（未使用/未声明依赖）、updates（可升级版本）、effective_pom（完整解析 POM）。
- 支持 includes 过滤、offline 模式（避免内网远程仓库超时）。
- 注册到 `core` toolset。

### node_version 工具
- Node.js 版本管理（nvm-windows/fnm/volta/n 自动检测）。
- action: list/current/available/install/uninstall/use。
- 注册到 `core` toolset。

### toolsets.py 调整
- `web` toolset 加入 `http`。
- `core` toolset 加入 `maven_dep`、`node_version`。

## 变更二：web 工具 check_fn 门控（`tools/web_tools.py`）

### 背景
web_search 需要外部搜索 API key（Tavily/SerpAPI/Bing/Firecrawl），未配置时每次调用都报错，白白消耗 system prompt token。web_extract/web_crawl 在 airgapped 内网环境只有 Firecrawl 路径可用，其 local fallback 对内部 IP 有 SSRF 限制。URL 获取能力已被 http 工具覆盖。

### 改动
- `web_search` 的 check_fn 从 `_check_web` 改为 `_has_search_key`：无任何搜索 API key 时自动隐藏。
- `web_extract`/`web_crawl` 的 check_fn 改为 `_has_firecrawl`：无 Firecrawl 配置时自动隐藏。
- 效果：airgapped 部署下这三个工具不再出现在 system prompt 中，减少 token 开销。

## 变更三：gateway / file / mcp / cron / wecom 增强

### gateway/server.py（+44行）
- 消息处理逻辑调整。

### tools/file_tools.py（+95行）
- 文件工具增强。

### tools/mcp_tool.py（+48行）
- MCP 工具增强。

### tools/cronjob_tools.py（+89行）
- cron 工具改进。

### platforms/wecom.py（+48行）
- 企微适配器改进。

### core/prompts.py（+51行）
- system prompt 增强。

### config.yaml
- 新增配置项。

## 影响面

- 新工具对所有模式立即可用（重启 gateway 后）。
- web 工具门控：已有 API key 配置的环境不受影响；airgapped 环境减少 3 个无用工具的 prompt 开销。
- 无破坏性变更，向后兼容。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — 工具注册表与调用链概念
