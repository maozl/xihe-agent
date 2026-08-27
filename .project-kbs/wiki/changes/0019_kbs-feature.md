---
type: change
title: 新增 KBS 子系统——可插拔业务知识库协议
slug: 0019_kbs-feature
change_type: feature
risk_level: medium
status: completed
created: 2026-07-30
updated: 2026-07-30
affected_modules:
  - core/config.py
  - core/prompts.py
  - core/agent.py
  - core/toolsets.py
  - gateway/server.py
  - tools/kbs_tool.py (新增)
  - core/kbs_protocol.md (新增)
  - core/kbs_templates/ (新增)
  - config.yaml
related_concepts:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/concepts/0014_project-context-loading.md
---

# 新增 KBS 子系统——可插拔业务知识库协议

## 摘要

给 xihe 增加一个**功能点**:可按需启用"业务知识库维护协议"(`.biz_kbs/AGENT.md`:受控词表 + 主域/多值 domain 归属 + 候选缓冲生命周期 + 保守自主边界)。一个总开关 `kbs.enabled` 同时门控**前导注入**和**工具可见性**;关闭则**零足迹**(无前导、工具消失)。读写知识库复用已有的 `core` 文件工具,只新增 `kbs_init`/`kbs_status`/`kbs_search` 三个 KBS 专用工具。

## 背景:两个知识库根

仓库并存两个 Atomic Knowledge 协议家族的知识库,**定位不同、不互迁**:

| 根 | 用途 |
|---|---|
| `.project-kbs/`(本页所在) | xihe **代码项目**的架构/进度/踩坑 |
| `.biz_kbs/` | **业务知识库**(企业平台:数据地图/SQLScan/DPM…) |

**本功能解决的是"让 xihe 能用 `.biz_kbs` 这套业务协议"**——它是 xihe 的代码功能,所以记录在本库的 `changes/`,而不是 `.biz_kbs`。

## 配置与开关

```yaml
kbs:
  enabled: false      # 总开关。false = 完全无 KBS 足迹
  root: .biz_kbs      # KBS 根,相对仓库根解析(同 agent_home 做法)
```

`core/config.py`:`"kbs"` 进 replace 元组 + setdefault 元组两处;`resolve_repo_path()` 把相对 root 解析到仓库根。**一个开关管两件事**——前导注入(`agent.py` 读 config)+ 工具可见性(`check_fn`)。

## 改动内容

### 前导注入(精简版协议)
新增 `core/kbs_protocol.md`(~95 行精简版,**固定打包、不走配置**)。`load_kbs_preamble(root)`(`prompts.py`)读取并把 `<root>` 占位替换成绝对路径;`build_system_prompt(kbs_preamble=)` 在 identity 后插入;`_build_system_prompt`(`agent.py`)启用时生成、**仅主 agent**(非子 agent)。

### kbs 工具集(`tools/kbs_tool.py`,`toolset="kbs"`,`check_fn=_check_kbs`)
- `kbs_init`:从 `core/kbs_templates/` 盖章建空库;根非空且非 `force` → 拒绝覆盖;`subagent_blocked=True`。
- `kbs_status`:freshness/健康摘要(lint-status + active + recent + 过期提醒),会话启动按前导指示调。
- `kbs_search`:**先查 `wiki/index.md` + 域注册表**,命中给 title/type/domain/path/summary;未命中才全文兜底(强制 index-first)。
- 读写知识库文件复用 `core` 的 `read_file`/`write_file`/`search_files`/`patch`,**不重造 raw I/O**。
- `core/toolsets.py`:加 `kbs` 叶子 + 进 `full` includes;**不**进 `DEFAULT_TOOLSETS`(opt-in)。
- `gateway/server.py`:`kbs.enabled` 时把 `"kbs"` 追加进 toolset scope(镜像 mcp 追加;CLI 全开不受影响)。

### 打包模板(`core/kbs_templates/`,19 文件)
`AGENT.md`(完整协议,**根路径去硬编码**)+ 6 schema + 清零 `lint-status.json` + 空注册表/入口页 + `.gitkeep`。`init_kbs(root, force)` 递归盖章,加载用 `Path(__file__).parent.parent/"core"/"kbs_templates"`(同 browser_tool/skills_tool 打包惯例)。

## 接入点(改动文件)

| 文件 | 改动 |
|---|---|
| `core/config.py` | `kbs` 段两个元组 + `resolve_repo_path()` |
| `core/prompts.py` | `build_system_prompt(kbs_preamble=, cwd=)` + `load_kbs_preamble()` |
| `core/agent.py` | `_build_system_prompt` 读 kbs 配置、解析 root、传前导(仅主 agent) |
| `core/toolsets.py` | `kbs` 叶子 + `full` includes |
| `gateway/server.py` | `kbs.enabled` 时条件追加 `"kbs"` 进 scope |
| `tools/kbs_tool.py` | 新模块:`_check_kbs` + `init_kbs` + 3 handler + register |
| `core/kbs_protocol.md` | 精简版协议前导(固定打包) |
| `core/kbs_templates/` | 空白脚手架模板 |
| `config.yaml` | 注释示例段(默认关) |

## 设计决策

- **精简前导 vs 整段内联 `AGENT.md`(382 行)**:选精简——系统提示词 per-session 构建一次、快照复用 + API 前缀缓存,稳定前导每轮成本是缓存读取;精简保留全部行为触发(意图识别表/自主边界/检索纪律/写入纪律),完整步骤/schema 留在 `<root>/AGENT.md` 与 `meta/schemas/`,写入前 `read_file` 取。单一事实源、不漂移、基线 ~2k token。
- **`kbs_init` 做成工具(而非 CLI 子命令/LLM 徒手建)**:确定性模板盖章器,LLM 只调用不设计结构;避免徒手建树慢/不稳/违反"结构性变更需确认"。
- **复用 core 文件工具,只加 3 个专用工具**:xihe 已有完整本地文件 I/O,KBS 读写直接用;专用工具只补 raw I/O 做不好的(盖章、健康摘要、index 检索)。
- **`kbs_search` 是证据驱动后加的**:初版只有 init+status,日志实测发现 agent 查询时绕过 index 直接 `search_files`(裸 grep),遂加 `kbs_search` 强制 index-first、grep 仅兜底——"先不做、等证明需要再加"。
- **gateway 不注入 cwd / kbs 前导只主 agent**:gateway 的进程 cwd 是偶然属性、非语义工作区;前导跳过子 agent(结构性维护是主 agent 的事)。

## 踩坑 / 注意

- **per-session 系统提示词缓存**:改 `kbs.enabled`/前导只对**新会话**生效;老会话需 `/reset` 或等压缩触发重建。gateway 改动需**重启**。
- **`check_fn` 才是活门控**:`select_toolsets()`(config 驱动 scope)定义了但**休眠未用**;工具可见性靠 `check_fn`,别走那条死路径。
- **`tool_result(data, **kwargs)` 传了位置 dict 就忽略 kwargs**:`message` 必须塞进 dict 里,不能 `tool_result(d, message=...)`。
- **`AGENT.md` 模板去硬编码**:原 `.biz_kbs/AGENT.md` 写死 `E:\xihe-agent\.biz_kbs`,模板版改成"根目录由 `kbs.root` 决定"。
- **gateway toolset 收窄坑**:gateway 从 `DEFAULT_TOOLSETS` 起,`kbs` 不在默认表,须在 `gateway/server.py` 像 mcp 那样**条件追加**,否则即便 enabled 工具也路由不到。

## 验证

`py_compile` 全过 + 功能测试:`kbs_search` 在真实 `.biz_kbs` 上 WTSS→index 命中 big-data 域、数据地图→10 条(别名路由)、未命中→兜底 grep 分支、gate(disabled→0 工具 / enabled→3 工具)。日志实测:16:40 那轮 agent 用 `kbs_search` 走索引、不再裸 grep `.biz_kbs`(修复前是直接 `search_files`)。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — 工具注册 + check_fn 门控(机制基础)
- [[0013_toolset-scope-and-dynamic-expansion]] — toolset 分层 + 注册即列出
- [[0011_gateway-architecture]] — per-message agent + 系统提示词缓存(前导生效时机)
- [[0014_project-context-loading]] — 项目上下文加载(kbs 前导是另一层"告诉 agent 该干什么")
- [[0001_xihe-agent]] — 项目总览(已补 kbs 功能点)
