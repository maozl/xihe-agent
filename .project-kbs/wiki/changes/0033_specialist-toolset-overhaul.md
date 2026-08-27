---
type: change
title: 专家 Agent 落地 + 工具集目录重构
slug: 0033_specialist-toolset-overhaul
change_type: feature
risk_level: medium
status: completed
created: 2026-08-16
updated: 2026-08-16
affected_services:
  - xihe-agent
affected_modules:
  - core/agent_defs.py
  - core/toolsets.py
  - core/agent.py
  - tools/specialist_agent_tool.py
  - tools/mcp_tool.py
  - gateway/serve.py
  - desktop SettingsPanel/serveClient
rollback_plan: 单 commit 回退；agents/*.yaml 为新增目录不影响存量
related_insights:
  - wiki/concepts/0032_specialist-agents.md
---

# 专家 Agent 落地 + 工具集目录重构

## 摘要

两件交织的事（2026-08-16 完成）：①专家 agent 从「config.yaml 里的 agents section」迁到**每专家一个文件** `<agent_home>/agents/<slug>.yaml`，配 serve CRUD + 桌面可视化编辑器，并补齐 mcp 按服务器授权、skills「不选=不配置」语义；②**工具集目录整体重构**——删组合预设与 includes 机制、删死组 browser_scripts、`core` 四拆、`agent` 拆出 `skills`，形成 14 个带中文标签的平铺组。稳定架构知识见 [[0032_specialist-agents]]，本页记变更过程与影响面。

## 变更内容

### 1. 存储：config.yaml agents section → agents/*.yaml

- `core/agent_defs.py` 新增 `save_raw`（tmp+rename 原子写）/`delete_raw`/`list_raw_specs`（未校验编辑器视图）
- `_parse_def` 校验：`mcp-` 前缀保留（未注册告警不丢）、未知静态组告警剔除、toolsets 空回退 `DEFAULT_TOOLSETS=["files","memory"]`
- `config.example.yaml` 的 agents section 改为注释模板指向 agents/*.yaml

### 2. serve CRUD + 桌面编辑器

- `serve.py`：`GET /specialists`（spec 剥 api_key + `api_key_set` 布尔 + toolset 目录含 label/description/工具数 + mcp 服务器 connected 状态 + 活 registry 已注册名单）、`PUT /specialists/{slug}`（slug 正则挡路径穿越；api_key 三态：非空写/空串清/缺省保持）、`DELETE`（幂等）。旧 `_write_agents_section` 删除
- 桌面 `SpecialistsCard`：行内 CRUD 表单；工具集 chips（按 label 显示，title 带组名+描述+工具数）；MCP 区独立 chips（全部 + 每服务器 `mcp-<server>`，未连接虚线；目录外的旧名虚线保留）；技能白名单 chips（catalog ∪ 旧名虚线保留）；模型与连接覆盖四个只写/留空继承字段
- **待重启徽标**：`registered` 读活 registry，文件已改但 serve 未重启时列表标「待重启」

### 3. skills 空集语义修复（用户报告 bug）

症状：专家「不选技能」实际拿到**全量技能索引**。根因：`skills_allowed` 传递链上两处 falsy 翻转（`or None` 一类）把空集合变成 None（None=全量）。修复：`core/agent.py` 构造参数显式 `set(...) if ... is not None else None`；`specialist_agent_tool` 传 `set(agent_def.skills)`。e2e 增加 `skills_allowed == set()` 断言。

### 4. 工具集目录重构（用户逐轮裁决）

演进顺序（每步都是用户明确选择）：

1. **删组合预设** debugging/safe/research/coding/full——grep 证实零运行时消费者，纯让人费解
2. **删 browser_scripts**——与 web 内容重叠的死组（零引用）
3. **拆 `agent`**：委派（delegate_task/todo/external_agent/model_info）与技能（skills_list/skill_view/skill_manage）分开——技能主/子 agent 都可能用，委派只有主 agent 用
4. **删 includes 机制**——组合没了，递归展开无人使用，`resolve_toolset` 打平为单层查表
5. **`core` 四拆**：files（含补列的 directory_tree）/terminal/dev_tool（用户命名，否决了 code）/http——「core 里的工具不是一个类别」

最终 14 个平铺组（均有中文 label + description）：

| 组 | label | 工具 |
|---|---|---|
| files | 文件 | read_file, write_file, search_files, directory_tree, patch |
| terminal | 终端与进程 | terminal, process |
| dev_tool | 代码与环境 | execute_code, maven_dep, node_version |
| http | 网络请求 | http, request_tools |
| web | 网页与搜索 | 浏览器全套 + 搜索/抓取 |
| memory | 记忆 | memory, session_search |
| communication | 消息通知 | send_message, send_image, clarify |
| media | 图像与语音 | vision_analyze, image_ocr, image_generate, text_to_speech |
| agent | 委派 | delegate_task, todo, external_agent, model_info |
| skills | 技能 | skills_list, skill_view, skill_manage |
| scheduler | 定时任务 | cronjob |
| mcp | MCP 工具 | 全部 MCP（动态填充） |
| ssh | SSH 远程 | ssh_connect/exec/disconnect/status |
| kbs | 业务知识库 | kbs_init/status/search |

`DEFAULT_TOOLSETS = [files, terminal, dev_tool, http, memory, communication, agent, skills, ssh]`；`delegate_tool` 的 DEFAULT_TOOLSETS 同步为 `[files, terminal, dev_tool, http, web, memory, media]`。

### 5. mcp_tool 同步收口

`_sync_mcp_toolsets` 原先同时填 `mcp` 全量组和每服务器组，现只填 `mcp`；每服务器 `toolset=f"mcp-{name}"` 注册不变（靠 get_schemas 双路匹配生效）。

### 6. 存量迁移

用户机 `~/.xihe-agent/agents/itsm.yaml`：`core`→四组 1:1 展开，删死组 `browser_scripts`。

## 变更分析

### 动因

- 每专家一文件：可独立增删、原子写、编辑器无需整段重写 config；slug 即文件名即工具名，一条链
- mcp 按需：全有或全无太粗，`mcp-<server>` 让专家只拿该拿的服务器
- 目录重构：旧组名（core/agent/组合）从名字看不出实际作用；工具集选择是专家编辑器的主要交互，目录即 UI 文案

### 破坏性影响（改组名 = 破坏配置兼容）

- 所有引用旧组名的 YAML（专家文件/delegate 参数）失效：静态未知组**告警剔除**（不崩，回退默认），旧 `core` 引用静默变 files+memory 意外收窄——所以存量 itsm.yaml 必须迁移
- 工具模块 register 的 `toolset=` 字符串 9 个文件批量重定向（file_tools×5、terminal/process、execute_code/maven/node_version、http/request_tools、model_info→agent、skills 三件→skills）；`tests/test_agent_loop.py` 假工具 core→files
- 桌面三处默认 `['core','memory']`→`['files','memory']`

## 验证

- 专项 e2e（temp AGENT_HOME + FakeReq + mock XiheAgent）：28 项全过——加载/校验/tool_name/config_overrides 合并不改父/mcp-* 保留/CRUD 全含 api_key 三态与 slug 拒绝
- `pytest`：12 passed
- `desktop npm run build`：三 bundle 绿
- grep：`src/` 下无残留 `"core"` toolset 引用（kbs_tool 的 `"core"` 是目录路径，无关）

## 相关页面

- [[0032_specialist-agents]] — 本变更沉淀的稳定架构概念页
- [[0013_toolset-scope-and-dynamic-expansion]] — 旧目录描述（已加订正注记）
- [[0002_tool-registry-and-dispatch]] — 注册表基础设施
