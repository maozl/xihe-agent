<p align="center">
  <img src="assets/xihe-readme-banner.jpg" alt="xihe-agent" width="100%">
</p>

# xihe-agent

<p align="center">
  <img alt="License: MPL-2.0" src="https://img.shields.io/badge/license-MPL--2.0-blue">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="OpenAI-compatible" src="https://img.shields.io/badge/OpenAI-compatible-brightgreen">
  <img alt="Platforms" src="https://img.shields.io/badge/platforms-CLI%20%E2%80%A2%20WeCom%20%E2%80%A2%20Feishu%20%E2%80%A2%20Desktop-orange">
</p>

*羲和,《楚辞》中为太阳驾车的神 —— 昔驭日,今驭工具。*

**单进程、OpenAI 兼容的 tool-calling agent —— 一个核心,四种形态:交互式 CLI、消息网关、HTTP+WS 服务、Electron 桌面应用。**

[English](README.md) | [简体中文](README.zh-CN.md)

接上任意 OpenAI 兼容端点(智谱、火山、DeepSeek、OpenAI 或内网网关),xihe 就在终端、企微/飞书、HTTP 服务与桌面应用之间共享同一套工具、技能、记忆与配置。它不只是回答问题 —— 它带着浏览器登录态替你操作内网系统、跑终端、读写文件、执行定时任务,并且把每一步过程摊开给你看。

```
                 ┌──────────────────────────────────────┐
                 │            一个 agent 核心            │
                 │   XiheAgent · 工具注册表 · 会话 ·     │
                 │   技能 · 审批 · 记忆 · 压缩器 · cron  │
                 └──────┬──────────┬──────────┬─────────┘
                        │          │          │
          ┌─────────────┘          │          └─────────────┐
    xihe chat                 xihe gateway            xihe serve
    交互式 CLI                企微 / 飞书              HTTP + WebSocket
    一次性查询                聊天消息驱动的           桌面应用与外部
    命名会话                  agent bot、斜杠命令、     客户端的服务接口
    会话恢复                  入站图片自动识别
                                                      ┌──────────────────┐
                                                      │ desktop/ Electron│
                                                      │ 桌面控制面        │
                                                      └──────────────────┘
```

## 为什么是 xihe

**🧠 一个大脑,四种形态。** agent 循环、80+ 工具、技能、审批由 `xihe chat`、`xihe gateway`、`xihe serve` 和桌面应用共享 —— 配置一次,处处运行;长期记忆跨端共享,它对你的了解不随入口变化(会话历史按平台各自隔离)。

**🌐 带着登录态替你上网。** xihe 用 CDP 驱动一台专属的真实 Chrome(独立 profile、独立调试端口):你扫一次码、过一次 SSO,登录态跨 xihe 重启持久保留,之后它就一直"记得"你的内网系统。真实 Chrome 自带 HSTS 记忆,SSO 回调里的 Secure cookie 不会像全新 Playwright context 那样静默丢失 —— 企业单点登录不再无限循环。

**👀 过程透明,随时插手。** 思考与回复分离呈现;企微里实时滚动思考摘要与工具执行流,回复到达时整条替换,像折叠了一样干净。任何时刻 `/stop` 打断;任务运行中发新消息会作为 steer 在下一个迭代边界注入,不打断、只纠偏。

**🛡️ 危险操作有人把关。** 39 条危险命令模式 + 高危参数表 + LLM 语义判定,三端一致的人工确认:CLI 提示、桌面审批卡、聊天回复 `y|n|a`。`deny`/`allow` glob 规则与会话级「批准且不再询问」让闸门不变成噪音。

**🎬 录一次,就会了。** `browser_record` 把浏览器操作录成带 role/name 元数据的动作与可运行的 Playwright 脚本;`web-record-to-skill` 技能进一步把录制沉淀为可回放的技能。agent 自己探索网站时也能边操作边录(`browser_record_start/stop`)—— 人和 agent 的操作,同一套录制器都认。

**🧑‍💼 专家 agent 各司其职。** 在 `~/.xihe-agent/agents/` 放一个 YAML,主 agent 就多一个 `run_<slug>_agent` 派发工具:带自己的 persona、自己的工具与技能白名单,还能单独覆盖模型连接 —— 例行巡检跑便宜模型、代码攻坚跑旗舰模型,一个 xihe 里分工明确。与 `delegate_task` 的临时子代理不同,专家跑完整分层提示词;桌面端提供可视化编辑器。

**🤝 还能指挥别的 agent。** `external_agent` 把子任务整体交给外置 CLI agent(claude)执行,凭据直接复用 xihe 的模型连接(自动换算为对应的环境变量),不用再单独配一套 —— xihe 做调度,外置 agent 做外援。

**📚 越用越懂你的业务。** `.biz_kbs` 业务知识库协议:按业务域组织的活知识库 —— 原始材料仅追加可溯源,wiki / 候选暂存分层,受控词表定归属。只在用户明确说「收录 / 记下来」时才写入,普通问答分析不动库;之后任何会话 `kbs_search` 一查,之前的调研结论都还在。

**🧩 技能按需注入,MCP 热插拔。** 提示词只带技能索引,正文用时 `skill_view` 才读进上下文;MCP 服务器 `/reload-mcp` 免重启热加载,并可按 agent 挂载不同子集;任务跑到一半,agent 还能经 `request_tools` 现场申请 `web` / `media` / `scheduler` 工具面 —— 平时精简,用时在场。

**🔌 任意 OpenAI 兼容模型。** 智谱、火山、DeepSeek、OpenAI、内网网关,换个 `base_url` 即可。`/model` 自动发现端点提供的全部模型;常见型号的上下文长度按内置目录解析,压缩阈值不用手配。

还有更多:17 个工具集、运行时创建技能、任务委派、SQLite 会话与崩溃恢复、长期记忆、定时任务、能力商店、多实例并行 —— 见下文。

## 与典型单入口 agent 框架的对比

| 维度 | xihe-agent | 典型单入口 agent |
| --- | --- | --- |
| 入口形态 | CLI + 企微/飞书网关 + HTTP/WS 服务 + 桌面应用,共享同一个核心 | 通常只有一种(CLI 或网页) |
| 操作已登录的内网系统 | CDP 驱动真实 Chrome,SSO 登录态跨重启持久保留 | 通常不支持 |
| 模型提供方 | 任意 OpenAI 兼容端点(智谱、火山、DeepSeek、OpenAI、内网网关) | 常绑定单一厂商 |
| 危险操作闸门 | 39 条模式 + 高危参数表 + LLM 判定,三端一致确认体验 | 各不相同 |
| 长期记忆 | 四个入口共享,按 agent 命名空间隔离 | 往往仅限单会话 |
| 业务知识库 | `.biz_kbs` 活知识库,原始材料仅追加可溯源 | 通常没有 |
| 专家 agent | YAML 定义,可配独立模型/工具/技能 | 通常只有临时子代理 |

## 60 秒上手

```bash
git clone <repo-url> && cd xihe-agent
pip install -e .            # 或: pip install -r requirements.txt
xihe                        # 首次启动自动生成 ~/.xihe-agent/config.yaml(带完整注释)
```

打开 `~/.xihe-agent/config.yaml`,填入 `api_key`、按需调整模型连接和工具集:

```yaml
model: glm-4.6              # 任意 OpenAI 兼容模型
base_url: https://open.bigmodel.cn/api/paas/v4/
api_key: sk-...
toolsets: ["files", "terminal", "web", "http", "memory", "mcp", "kbs"]
skills: ["*"]                # 注入完整技能索引; [] = 不注入
```

开始对话:

```bash
xihe                          # 交互式聊天(默认子命令)
xihe chat -q "总结一下当前目录"   # 一次性查询,非交互
xihe chat -s my-project      # 命名会话
xihe chat -r                  # 列出并恢复最近的会话
```

首次启动无需其他准备:`config.yaml` 不存在时,xihe 按模板自动生成一份带完整注释的默认配置并提示填入 `api_key`(不进入对话);桌面端则显示欢迎卡片,引导到「设置」页完成配置。

## 环境要求

- Python ≥ 3.10
- 一个 OpenAI 兼容的 chat-completions 端点(模型名、`base_url`、`api_key`)
- 按功能可选:Playwright(浏览器工具)、PaddleOCR/PaddlePaddle(离线 `image_ocr`)、搜索 API key(`web_search`)

## 配置

`~/.xihe-agent/config.yaml` 是唯一的配置源 —— 完整注释参考见 [`config.example.yaml`](config.example.yaml)。要点:

| 键 | 含义 |
| --- | --- |
| `model` / `base_url` / `api_key` | 主 agent 连接(OpenAI 兼容) |
| `toolsets` / `skills` | 主 agent 名单:`[]` = 无工具、`["*"]` = 全量、名单 = 白名单 |
| `models` | 模型目录:为型号登记 `context_length`(优先于内置目录);`/model` 列表 = 本段 ∪ 端点自动发现 |
| `vision_model` | `vision_analyze` 使用的多模态模型(主模型可以纯文本) |
| `max_iterations` / `compression_threshold` | agent 循环预算与上下文压缩触发阈值 |
| `specialists.enabled` | 专家委派总开关(默认关) |
| `platforms.wecom` / `platforms.feishu` | 网关适配器凭据 |
| `mcp_servers` | 按名注册的 MCP 服务器(`streamable-http` / `stdio`) |
| `approvals` | 危险操作闸门:`mode`、`timeout`、`deny`/`allow` 规则、`llm_judge` |
| `auxiliary` | 视觉/图像生成/TTS/审批判定的独立模型 |
| `web` | 搜索/抓取 API key(tavily、serpapi、bing、firecrawl)—— 留空则对应工具隐藏 |
| `store.sources` | 能力商店索引 URL(HTTP 或本地路径) |
| `kbs.enabled` | 业务知识库(`.biz_kbs`)工具 |
| `agent_home` | 实例数据根(仅在 `--config` 实例文件里有意义) |

## 运行模式

### CLI —— `xihe chat`

```bash
xihe                          # 在当前目录启动交互式 REPL
xihe chat -s bug-hunt -q "找出不稳定的测试并提出修复方案"
xihe chat -r                  # 恢复之前的会话
```

agent 的工作目录就是启动目录;该目录下的 `CLAUDE.md` / `xihe.md` / `AGENTS.md` / `.cursorrules` 存在时会注入系统提示词(经 `session.*` 开关控制)。

### 消息网关 —— `xihe gateway`

把聊天消息变成 agent 回合,支持企微(WebSocket)与飞书:

```bash
xihe gateway                       # 平台来自配置
xihe gateway --platform wecom
```

入站图片在到达纯文本主模型之前,自动经视觉/OCR 描述。以 `/` 开头的消息是斜杠命令,在进入 agent 之前处理。任务运行中发来的普通消息作为 steer 注入当前回合(下一迭代边界生效),`/stop` 或一句「停止」立即打断。网关是长驻进程 —— **改代码后需重启生效**。

### HTTP + WebSocket 服务 —— `xihe serve`

同一个 agent 核心,由 aiohttp 前置,供桌面应用或任何外部客户端驱动:

```bash
xihe serve                         # 127.0.0.1:7788
xihe serve --host 0.0.0.0 --port 7788
```

REST 接口包括 `/health`、`/readiness`(结构化的缺配清单)、`/test-connection`(服务端模型连通性探测 —— api_key 不出服务)、`/agents`、`/sessions`、`/convs/{id}/messages`、`/convs/{id}/trace/{msg}`、`/mcp`、`/skills`、`/cron`、`/specialists`(CRUD)、`/store`(安装/卸载/挂载)、`/browser/*`(内嵌浏览器控制);`/stream` 是 WebSocket 聊天通道,按轮次推送思考流、文本流、工具调用与审批事件。

### 环境体检 —— `xihe doctor`

一条命令输出可操作的清单 —— 每个失败项都直接给出修法(配置字段或安装命令):

```bash
xihe doctor            # 配置、依赖、浏览器、能力矩阵、MCP、连通性
xihe doctor gateway    # 额外检查平台凭据
```

同一套探测支撑 `GET /readiness`。

### 桌面 —— `desktop/`

Electron + React + Tailwind 控制面(独立 Node 工具链,与 Python 核心零代码共享)。它从 `PATH` 拉起 `xihe` CLI(`XIHE_BIN` 可覆盖),托管 serve 进程生命周期,并经 IPC 编辑 `~/.xihe-agent/config.yaml`:

```bash
cd desktop
npm install
npm run dev      # 开发窗口
npm run build    # 类型检查 + 打包
```

桌面端提供:工作区(把会话绑定到项目目录)、内嵌浏览器面板(Chrome 吸附进窗口、亮暗主题跟随)、能力商店(浏览/安装/挂载技能与 MCP)、专家 agent 编辑器、设置页。

> 内网/气隙环境:需自行在全局 `~/.npmrc` 配置 npm registry 与 `electron_mirror` —— 否则 Electron 二进制的 postinstall 会去 github.com 下载,离线时失败。

## 工具与工具集

每个工具在 import 时自注册;`src/core/toolsets.py` 负责分组:

| 工具集 | 内容 |
| --- | --- |
| `base` *(自动并入)* | `read_file`、`search_files`、`directory_tree`、`skills_list`、`skill_view`、记忆读取、`kbs_search`、`todo`、`model_info`、`run_sandbox_code`(RestrictedPython) |
| `web` | `web_search` / `web_extract` / `web_crawl` + 40 余个 `browser_*` 自动化工具(导航、点击、输入、标签页、iframe、Cookie、截图、登录态保存/恢复、操作录制) |
| `files` | `write_file`、`patch` |
| `terminal` | `terminal`、`process` |
| `dev_tool` | `execute_code`、`maven_dep`、`node_version` |
| `http` | `http`、`request_tools` |
| `memory` | `memory_manage`、`session_search` |
| `communication` | `send_message`、`send_image`、`clarify` |
| `media` | `vision_analyze`、`image_ocr`、`image_generate`、`text_to_speech` |
| `agent` | `delegate_task` |
| `external_agents` | `external_agent`(claude CLI) |
| `skills` | `skill_manage` |
| `scheduler` | `cronjob` |
| `ssh` | `ssh_connect`、`ssh_exec`、`ssh_disconnect`、`ssh_status` |
| `kbs` | `kbs_init`(检索/状态在 `base` 组) |
| `meta` | `request_tools`(运行时按需请求 `web`/`media`/`scheduler`) |
| `mcp` / `mcp-<server>` | 全部 / 单台 MCP 服务器的工具 |

一个 agent 的工具面 = **base ∪ roster − blocked**:每个非空名单自动并入只读底座 `base`(文件读、技能索引、记忆读、进程内计算沙箱),写入类与重型能力按名单授予,递归/用户界面类工具从所有子代理剥离。`check_fn` 是可用性闸门:Playwright 不可导入时浏览器工具整体消失;没有 API key 时搜索工具隐藏。

## 危险操作审批

`approvals` 把破坏性操作挡在三值决策管线(`allow / ask / deny`)之后:

```yaml
approvals:
  mode: manual            # manual = 危险操作需确认 | auto = 全放行
  timeout: 300            # 等待答复的秒数
  timeout_action: deny    # 超时后的处理
  llm_judge: true         # 正则漏网的命令用辅助模型语义判定
  deny:                   # 硬闸门,命中即拒不弹窗
    - "terminal(*mkfs*)"
    - "ssh_exec"
  allow:                  # 持久白名单
    - "terminal(rm -rf /tmp/*)"
```

规则语法为 `"tool(glob)"` —— `terminal`/`ssh_exec` 的 glob 匹配命令原文,其余工具匹配 `action` + 关键参数。决策顺序:`mode: auto` > `deny` 规则 > `allow` 规则 > 会话记忆 > 危险判定。「批准且不再询问」(聊天回复 `a`、桌面第三个按钮)只在本会话内对同一危险类静默;跨会话的放行走 `allow`。无人值守的 cron 运行没有确认通道,直接拒绝 —— 定时任务必须跑危险命令就设 `mode: auto`。

启发式(39 条命令模式 + 高危参数表 + LLM 判定)是便利性闸门,**不是安全边界**。

## 技能

一个技能是一个目录:`SKILL.md`(YAML frontmatter:`name`、`description`)加可选的 `scripts/` 与参考文件。内置技能在 `src/skills/`,用户技能在 `~/.xihe-agent/skills/`。agent 经 `skills_list` / `skill_view` 列出与查看,经 `skill_manage` 创建与编辑;`web-record-to-skill` 技能还能把一次浏览器操作录制成可回放的技能。

## 专家 agent

`~/.xihe-agent/agents/` 下每个专家一个 YAML 文件(文件名 = slug),由 `specialists.enabled` 总闸控制:

```yaml
persona: "你是一名发布工程师..."
toolsets: ["terminal", "dev_tool"]
skills: []
# model / base_url / api_key / max_iterations —— 不写的键继承主配置
```

总闸开启后,每个文件注册一个 `run_<slug>_agent(goal, context)` 工具,主 agent 的提示词获得一层花名册,把工作路由过去。与 `delegate_task` 的纯任务卡子代理不同,专家 agent 跑完整分层提示词,带自己的 persona 与白名单,甚至可以连不同的模型端点。

## 会话、记忆、定时任务

- **会话**是 SQLite 行,按平台 + 聊天 + 用户确定性生成 key(`agent:main:cli:dm:...`)。历史在循环每次迭代后落库;加载时自动修复崩溃回合留下的悬空 tool call。每个会话可单独覆盖模型。
- **记忆**是长期性的、带命名空间(主 agent 与每个专家各自独立),每轮以快照注入。
- **cron** 任务持久化在 `~/.xihe-agent/cron/` 下,由全量 agent 执行:

```bash
xihe cron list
xihe cron create 30m "检查构建看板并汇报失败项" --name build-watch
xihe cron run <job_id>
xihe cron remove <job_id>
```

## 多实例

```bash
xihe --config ~/instances/support.yaml gateway
```

实例文件是该进程唯一的配置源;其可选的 `agent_home` 隔离数据根(`sessions.db`、`agent.log`、`browser/`、`cron/`、技能、`.biz_kbs`)。网关、serve、CLI 实例可互不干扰地并行运行。

## 架构

```
src/
├── core/          XiheAgent 循环、配置、会话、压缩器、提示词、工具集、模型目录
├── tools/         工具模块 —— 每个 import 时自注册进注册表
├── platforms/     企微(WebSocket)与飞书适配器,基于 BasePlatformAdapter
├── gateway/       bot.py(消息网关)· serve.py(HTTP+WS 服务)· 流式消费 · 斜杠命令
├── cli/           chat.py REPL + app.py 入口 / SharedContext
└── skills/        内置技能
desktop/           Electron 控制面(独立 Node 工具链)
tests/             pytest 套件 —— L0 纯函数、L1 工具(mock IO)、
                  L2 agent 循环不变式(注入假模型客户端)
```

贡献前值得了解的 agent 循环不变式:只读工具调用并发执行,任一写工具强制串行;消息在每次迭代后持久化到 SQLite;在 `max_iterations` 的 70%/90% 处注入催促/警告;超限的工具结果溢出到旁路存储而非内联进历史;`agent.interrupt()` 可从另一线程停止循环并传播到子代理。

## 开发

**添加一个工具 —— 两步都必须做:**

1. 在 `src/tools/*.py` 模块中注册:`registry.register(name, schema, handler, check_fn=..., toolset="...", read_only=...)`,import 时执行。handler 接收 `(args: dict, **kw)`(context 与 `parent_agent` 经 kwargs 传入),返回 JSON 字符串。
2. 在 `src/core/toolsets.py` 的工具集里列出它。注册了但未列出的工具对 agent 不可见。

**注意事项:**

- gateway 与 serve 是长驻进程 —— 改代码后需重启生效。
- `enabled_toolsets=[]` 表示「无工具」,`None` 表示「全量」。不要用 truthiness 判断把它们混为一谈。
- Playwright 不可导入时浏览器工具整体消失(`check_fn` 闸门)。
- 保持 `requirements.txt` 与 `pyproject.toml` 依赖列表一致。

**测试:**

```bash
pytest
```

## 许可证

[MPL-2.0](LICENSE) —— 文件级 copyleft:可自由使用、修改、分发,包括并入更大的闭源作品;只有你修改过的文件必须继续以 MPL-2.0 开源。
