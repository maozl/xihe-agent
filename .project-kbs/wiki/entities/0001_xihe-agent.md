---
type: entity
entity_type: service
title: xihe-agent
slug: 0001_xihe-agent
aliases:
  - xihe
tags:
  - agent
  - openai-compatible
  - cli
  - gateway
status: active
created: 2026-07-01
updated: 2026-08-20
related_pages:
  - wiki/changes/0019_kbs-feature.md
  - wiki/concepts/0023_multi-instance-config.md
sources:
  - path: ../../CLAUDE.md
    date: 2026-07-01
---

# xihe-agent

## 摘要

单进程、OpenAI 兼容的**工具调用 agent**，从同一核心运行两种模式：交互式 CLI（`xihe chat`）与消息**网关**（WeCom WebSocket、飞书），把聊天消息转换为 agent 轮次。Python ≥3.10。agent 循环、工具、会话、技能在两种模式间共享。

## 基本信息

- 语言: Python ≥3.10
- 入口: `cli.app:main`（`pyproject.toml` → `xihe` 脚本）
- 运行时状态: `~/.xihe-agent/`（可用 `AGENT_HOME` 覆盖）
  - `config.yaml` — 配置与凭据（**单源**，无 `.env`）
  - `agent.log` — 日志（网关在此输出）
  - `sessions.db` — SQLite 会话历史
  - `browser/states/*.json` — 浏览器登录态；`skills/` — 用户技能；`agents/*.yaml` — 专家 agent 定义（一文件一专家）
- 默认主模型: `glm-5.2-zp`（智谱，经 OpenAI 兼容内部网关）— **非多模态**

## 关键命令

| 命令 | 用途 |
|------|------|
| `xihe` / `xihe chat` | 交互式聊天 |
| `xihe chat -q "..."` | 单次查询（非交互） |
| `xihe chat -s <name>` | 命名会话 |
| `xihe gateway [--platform wecom]` | 运行消息网关（平台来自 config，默认 wecom） |
| `xihe --config x.yaml <cmd>` | 用指定实例配置启动（数据根 + 配置隔离，见 [[0023_multi-instance-config]]） |
| `xihe cron list` | 定时任务（create / remove / run） |

## 核心架构

- **工具注册表 + 工具集**（`src/core/toolsets.py`）: 每个 `src/tools/*.py` 在 import 时向 `registry` 注册；`XiheAgent` 按 `enabled_toolsets` 过滤暴露的 schema。新增工具必须**同时**注册并加入 toolset，缺一则对 agent 不可见。`check_fn` 是可用性门控（如 Playwright 不可导入时，所有 `browser_*` 工具消失）。
- **Agent 循环**（`src/core/agent.py`）: 标准 OpenAI 工具调用循环。只读工具并发（ThreadPoolExecutor）、写工具串行；每轮写回 SQLite；崩溃恢复（`_repair_dangling_tool_calls` / `_inject_recovery_hint`，不可剥离）；预算压力提示（70%/90%）；`ContextCompressor` 超阈值压缩；可中断。
- **两种模式共享同一 agent 核心**: CLI 用单个长生命周期 `XiheAgent`；网关（`src/gateway/bot.py`）每条入站消息构造**全新** `XiheAgent`（廉价），`SharedContext`（`src/cli/app.py`）持有昂贵对象（SQLite conn、`AuxiliaryClient`、`ContextCompressor`）。平台适配器在 `src/platforms/`（`WeComAdapter` / 飞书）。`/` 开头是斜杠命令（`src/gateway/commands.py`），图片入站先经视觉/OCR。
- **会话**（`src/core/session.py`）: SQLite，`SessionSource`（平台 + chat_id + 用户）→ 确定性 key（如 `agent:main:{platform}:dm:{chat_id}`）；会话 key 也是 cron / 取消单元。
- **配置单源**（`src/core/config.py`，2026-08-13 起）: 全部配置与凭据在**一个** config.yaml——`--config <path>` 指定的实例文件（import 时 peek `sys.argv`）或默认 `~/.xihe-agent/config.yaml`；值为字面量，**无 `.env`、无环境变量覆盖、无 `${VAR}` 展开**（旧「项目>用户>`.env`」分层已废）。数据根优先级：`~/.xihe-agent` < `AGENT_HOME` env < `--config` 文件的 `agent_home`（例外：`agent_home` 定位器本身支持 `${VAR}` 展开、相对路径按仓库根解析）。`xihe --config x.yaml` 选实例见 [[0023_multi-instance-config]]（其优先级链描述为单源化前形态）。
- **辅助 LLM 客户端**（`src/core/auxiliary_client.py`）: 工具内 LLM 调用（视觉分析、上下文压缩、标题生成）专用，区别于主模型。视觉 / 图像工具走这里。
- **技能**: 一个目录 `SKILL.md`（frontmatter: `name`/`description`）+ 可选 `scripts/`；bundled 在 `src/skills/`，用户技能在 `~/.xihe-agent/skills/`；由 `src/tools/skill_manager_tool.py` / `src/tools/skills_tool.py` 管理。
- **浏览器工具**（`src/tools/browser_tool.py`, Playwright）: **模块级全局状态**（`_page`/`_context`/`_browser_instance`）跨网关消息持久；优先用系统 Chrome/Edge（bundled Chromium 无法访问内网资源）。
- **专家 agent**（`src/core/agent_defs.py` + `src/tools/specialist_agent_tool.py`）: `agents/<slug>.yaml` 一文件一专家，启动时派生 `run_<slug>_agent` 工具（`agent` toolset，`subagent_blocked`）；persona 换身份层、toolsets/skills 白名单固定编制、连接键（model/base_url/api_key/max_iterations）留空继承主配置。与 `delegate_task` 临时子 agent 的分工及 skills 空集语义见 [[0032_specialist-agents]]。
- **KBS 子系统（业务知识库协议）**（`src/tools/kbs_tool.py` + `src/core/kbs_protocol.md`）: **功能点**，可选启用 `.biz_kbs/` 业务知识库协议。总开关 `kbs.enabled` 同时门控前导注入与工具可见性（`check_fn`），关掉即零足迹；新增 `kbs_init`（模板盖章建库）/`kbs_status`（健康摘要）/`kbs_search`（index-first 检索）三工具，文件读写复用 core 工具。见 [[0019_kbs-feature]]。

## 环境约束（本部署）

- **内网隔离**: 无公网，包从内部 PyPI 镜像装；浏览器用系统 Chrome/Edge，不依赖 `playwright install chromium`。
- **`requirements.txt` 是依赖真相源**（比 `pyproject.toml` 多列，如 `playwright`、`paddleocr`）。两者需保持一致。
- **主模型非多模态**: 视觉 / 图像任务必须走 `vision_model`（config 配置）或 `image_ocr`（PaddleOCR/PaddlePaddle，离线），不能假设主模型能看图。

## 常见陷阱

- 加工具 = 注册 **且** 加入 `src/core/toolsets.py` toolset；缺任一 agent 都用不到。
- Playwright 不可导入时浏览器工具整体消失（症状: "agent 没有浏览器工具"）。
- 网关是长驻进程，代码改动后需**重启**生效（schema、prompt、模块级浏览器状态在进程生命周期内缓存）。
- Playwright sync-API: 不要在事件回调（如 `framenavigated`）里调 `page.*`，会死锁 greenlet；改在 `add_init_script` JS 里做捕获，并 guard `sessionStorage`/storage 访问。
- `requirements.txt` 与 `pyproject.toml` 依赖需同步。

## 相关页面

- [[0023_multi-instance-config]] — 多实例配置（`--config` 启动时选实例）
- [[0011_gateway-architecture]] — 网关架构（SharedContext / 每消息薄 agent）
- [[0006_session-design]] — 会话两层 ID 设计
- [[0022_testing-strategy]] — 测试策略分层
- [[0019_kbs-feature]] — KBS 子系统
- [[0032_specialist-agents]] — 专家 agent（配置声明的常驻专家）
