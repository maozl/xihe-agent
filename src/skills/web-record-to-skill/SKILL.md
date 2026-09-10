---
name: web-record-to-skill
description: 把网页操作沉淀成可复用 skill。两种模式——人录(browser_record，用户操作页面) 或 agent 自主探索(browser_record_start→用 browser_* 探索→browser_record_stop)。用户说"录制网页操作/探索一下 XX 网站做成 skill/把站点功能摸一遍生成 skill/做个网页自动化 skill"等时，加载本 skill。
version: 1.9
---

# Web 操作录制 → 生成 / 更新 Skill

> ⚠️⚠️ **agent 探索模式（用户让你"探索/摸一遍/看看"网站并做成 skill）—— 第一步是 `browser_record_start(url=...)`，不是 `browser_navigate`！**
> 不先 `browser_record_start`，你后面用 `browser_*` 探索的动作就**录不到**，最后只能凭记忆写 skill（不准、易错）。
> 正确顺序：① 跟用户二次确认"是否让我自己探索" → ② `browser_record_start(url)` → ③ 用 `browser_*` **只读**探索 → ④ `browser_record_stop` 取结果 → ⑤ `skill_manage` 生成。
> （人录模式则用 `browser_record`，用户自己操作，你不要碰 browser_*。）

把网页操作录下来，再翻译成 `browser_*` 步骤写成（或更新）skill。先判断用哪种模式（见下）。

## 两种录制模式——先判断"谁来操作"

录制都在 agent 自己的 CDP Chrome（`browser_*` 用的那个，端口 9222）里进行。**开工前先判断这次该谁来操作页面**，这决定了用哪种模式：

| 用户的意思 / 场景 | 用哪种 | 工具 |
|---|---|---|
| "我录/我演示一遍/录下我的操作"、"你帮我录"（用户亲自点） | **模式 A：人录** | `browser_record(url)` |
| 流程要人工（SSO/2FA、只有用户会的步骤） | 模式 A | `browser_record` |
| agent 不熟站点、不敢乱点，让用户演示 | 模式 A | `browser_record` |
| "你探索/你自己摸一遍/agent 去操作/把站点功能都试出来" | **模式 B：agent 探索** | `browser_record_start` → `browser_*` → `browser_record_stop` |
| 要批量发现站点多个功能、agent 能自己 snapshot+导航 | 模式 B | `start` → `browser_*` → `stop` |

拿不准时**问一句**："这个流程是你来操作我录，还是让我自己去探索？" 再按回答选。

- **模式 A（人录）**：`browser_record(url)` 弹出 Chrome，**用户**手动操作，点右下角「完成录制」结束。
- **模式 B（agent 探索）**：**先跟用户二次确认**（"这是让我自己去探索这个站点/流程吗？"——拿到肯定答复再继续，别擅自开录）→ `browser_record_start(url)` 打开录制（不阻塞）→ **agent 自己用 `browser_*` 探索**（每步自动录下，**默认只看不改**，见下）→ `browser_record_stop` 取结果。

两种模式录到的 actions 格式完全一样（带 role/name + 语义选择器），后面翻译成 skill 的步骤通用。

> ⚠️ **铁律：一次录制只走一种模式，绝不混用。**
> - **模式 A**：调了 `browser_record` 后，**绝对不要再自己调 `browser_*`** 去操作页面——那会把 agent 的动作混进人录里污染录制。让它阻塞、等用户操作完返回即可。
> - **模式 B**：`browser_record_start` 之后**必须**用 `browser_*` 去探索（那是录制来源），探索完**一定要** `browser_record_stop` 收尾；忘了 stop 录制就一直开着。
> - 模式 B 注意：跨**整页刷新**（跳到不同网址）会丢之前步骤，尽量在同一 SPA 页面内操作（hash 路由切换不丢）；弹窗（新 tab）里的动作目前不录。
> - **模式 B 默认只做"查看"操作**：导航、看页面、打开详情、翻页、读列表/内容、点菜单/标签/链接去浏览——都可以。但**不要做改变数据的操作**：提交表单、创建/编辑/删除记录、审批/转派、点"保存·提交·删除·确认·通过"这类按钮——除非用户**明确**说"可以执行所有操作"或点名要某个写操作。要录的流程若含写操作，优先用模式 A（让人来录），或先问用户"是否允许我执行这个写操作"。

## 触发条件
用户说"录制网页操作做成 skill"/"把这个流程录下来"/"做个网页自动化 skill"/"更新 XX 的 skill"等。

## 前置条件
- 录制用的是 **agent 的 CDP Chrome（cdp-profile）**，和 `browser_*` 同一个浏览器。所以**目标站点要先在这个 Chrome 里登录**：先 `browser_login(url=...)` 或 `browser_navigate` 过去手动登录一次，cdp-profile 会记住。没登录就录，会撞登录墙。
- 跟用户确认：起始 `url`、业务 `description`、skill `name`（kebab-case）。

## 录制结束方式
点页面右下角蓝色的「⏹ 完成录制」按钮即结束（推荐）。调用后**告诉用户**："我已打开 Chrome 录制窗口（agent 那个浏览器），请操作；**完事后点右下角「完成录制」**。"
然后**停下等 `browser_record` 返回**，等待期间不要自己用 browser_* 操作，也不要发 /stop（会中断并丢录制）。

## 操作流程

### 1. 开始录制（核心，不可跳过）
- **模式 A（人录）**：`browser_record(url="<起始地址>", timeout=600)` —— 等用户操作完点「完成录制」返回。
- **模式 B（agent 探索）**：先 `browser_record_start(url="<起始地址>")` 打开录制；然后用 `browser_*` 自己把流程走一遍（每步自动录下）；最后 `browser_record_stop` 取出结果。

两种方式最终都拿到 `actions`（每条带 `type`/`css`/`selector`/`role`/`name`/`text`）和 `script`（等价 Playwright 脚本，存档用）。下面的翻译/生成 skill 步骤对两种模式都一样。

### 2. 用 `actions` 写 browser_* 步骤
**优先用 `actions`**（`role`/`name`/`text` 让你写好人话描述）。按 type 映射成 browser_*，**选择器用每条的 `css`**（录制时已验唯一）：

| action.type | browser_* 调用 | 描述用 |
|---|---|---|
| `goto` | `browser_navigate(url=...)` | url |
| `click` | `browser_click(selector=<css>)` | role/name/text |
| `fill` | `browser_type(selector=<css>, text=<value>)` | name + value |
| `select` | `browser_select(selector=<css>, value=<value>)` | name + value |
| `press` | `browser_press(key=<key>)` | name + key |
| `check`/`uncheck` | `browser_check`/`browser_uncheck(selector=<css>)` | name |
| `goto`(hash 跳转) | `browser_navigate(url=...)` | url |

**只写录制里真有的步骤**——别凭空补（要补就标"未验证，需实测"）。

### 3. 判断：更新已有 skill 还是新建
先 `skills_list` / `skill_view` 看是否已有相关 skill：
- **同一业务流程已存在** → 不要 create（撞名），改用 `patch`（局部）或 `edit`（大改）。
- **不同业务流程**（如"查工单列表" vs "看工单详情"，同一系统）→ 新建，名字按功能区分。
- **不存在** → `skill_manage(action="create", name="<name>", category="<按业务领域/系统选，如 itsm / cmdb / monitor / wiki>", content=<SKILL.md>)`。category 就是 skill 的分类目录名，用小写英文，代表这个 skill 属于哪个系统或领域。

### 4. 必做：存档录制脚本
把 `script` 归档进 skill：`skill_manage(action="write_file", name="<name>", file_path="references/recorded.py", file_content=<script>)`。更新时先把旧的留底为 `references/recorded-<时间戳>.py`。

### 5. 验证
`skill_view(name="<name>")` 确认；不准就 `patch`。可选：用 `browser_*` 实跑关键步骤验证选择器。

## ⚠️ 关键坑
- **录制在 agent 的 CDP Chrome 里**（cdp-profile）。目标站点要先在那里登录（`browser_login`），否则会跳登录页。登录态和 `browser_*` 完全一致。
- **选择器用 `actions` 里的 `css`**（录制时已验唯一）；优先稳定属性（`placeholder`/`name`/`data-testid`），别用框架动态数字 id。失效就重录或用 `browser_snapshot` 修。
- **框架页面输入必须用 `browser_type`**（真实键盘事件触发 v-model/受控组件）；`browser_eval` 设 value 框架不响应。
- **别重放录制脚本本身**（`chromium.launch()` 是脚本里的浏览器）。只取动作翻译成 `browser_*`。
- **别编造录制里没有的步骤。** 录到什么写什么；要补充就标注"未验证"。
