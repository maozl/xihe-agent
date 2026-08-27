# Browser 工具文档

## 概述

Xihe Agent 的浏览器工具基于 Playwright，提供完整的网页自动化能力。核心特性：

- **独立工具拆分**：每个浏览器操作都是独立工具，LLM 选择更精准
- **Accessibility Tree**：结构化页面快照 + ref ID 精准交互
- **三层认证架构**：持久化 Profile / StorageState / CDP 连接
- **完整交互能力**：wait / hover / select / drag / upload / eval / tabs / frames / cookies

## 工具列表

### 页面导航

| 工具 | 说明 |
|------|------|
| `browser_navigate` | 打开 URL |
| `browser_back` | 后退 |
| `browser_forward` | 前进 |
| `browser_reload` | 刷新页面 |
| `browser_close` | 关闭浏览器（持久化 profile 保留） |

### 页面快照

| 工具 | 说明 |
|------|------|
| `browser_snapshot` | Accessibility tree 快照，带 ref ID（[@e1], [@e2]...） |
| `browser_screenshot` | 截图保存为文件 |
| `browser_vision` | 截图 + Vision AI 分析 |

### 页面交互

| 工具 | 说明 |
|------|------|
| `browser_click` | 点击元素（优先用 ref ID） |
| `browser_type` | 输入文本（可选 submit=true 回车提交） |
| `browser_hover` | 悬停元素（触发下拉菜单、tooltip） |
| `browser_select` | 选择下拉框选项 |
| `browser_scroll` | 上下滚动 |
| `browser_press` | 按键（Enter, Tab, Escape 等） |
| `browser_drag` | 拖拽元素到目标位置 |
| `browser_check` | 勾选复选框 |
| `browser_uncheck` | 取消勾选复选框 |
| `browser_upload` | 上传文件 |

### 等待与同步

| 工具 | 说明 |
|------|------|
| `browser_wait` | 等待条件满足（文本/元素/URL/加载状态/JS函数/超时） |

### 标签页管理

| 工具 | 说明 |
|------|------|
| `browser_tab_new` | 新建标签页 |
| `browser_tab_list` | 列出所有标签页 |
| `browser_tab_switch` | 切换到指定标签页 |
| `browser_tab_close` | 关闭标签页 |

### 高级操作

| 工具 | 说明 |
|------|------|
| `browser_eval` | 执行 JavaScript 并返回结果 |
| `browser_frame` | 列出/切换 iframe |
| `browser_cookies` | 读取/设置/清除 cookies |

### 调试

| 工具 | 说明 |
|------|------|
| `browser_console` | 获取 console 日志和 JS 错误 |

### 认证管理

| 工具 | 说明 |
|------|------|
| `browser_state_save` | 保存当前登录态（cookies + localStorage） |
| `browser_state_load` | 加载已保存的登录态 |
| `browser_state_list` | 列出所有已保存的登录态 |
| `browser_state_delete` | 删除已保存的登录态 |
| `browser_connect` | 连接到用户正在运行的浏览器（CDP） |

## 基本工作流

```
1. browser_navigate(url)     → 打开页面
2. browser_wait(text="Welcome")  → 等待页面加载
3. browser_snapshot()        → 获取页面结构 + ref ID
4. browser_click(@e5)        → 用 ref ID 点击
5. browser_type(@e3, text)   → 用 ref ID 输入
6. browser_snapshot()        → 页面变化后重新快照
```

## browser_wait 详解

`browser_wait` 是最常用的同步工具，支持多种等待模式：

```python
# 等待文本出现
browser_wait(text="Welcome")

# 等待元素可见
browser_wait(selector=".result-item")

# 等待 URL 变化
browser_wait(url="**/dashboard")

# 等待页面加载完成
browser_wait(load_state="networkidle")

# 等待 JS 条件
browser_wait(function="document.querySelector('.loaded') !== null")

# 等待元素隐藏（消失）
browser_wait(selector=".loading-spinner", state="hidden")

# 自定义超时（毫秒）
browser_wait(selector=".slow-element", timeout=30000)
```

## browser_eval 详解

执行任意 JavaScript，适合数据提取和 DOM 操作：

```python
# 提取数据
browser_eval(expression="document.title")
browser_eval(expression="Array.from(document.querySelectorAll('.item')).map(e => e.textContent)")

# 操作 DOM
browser_eval(expression="window.scrollTo(0, document.body.scrollHeight)")

# 调用页面 API
browser_eval(expression="JSON.stringify(window.__NEXT_DATA__)")
```

## 标签页管理

```
# 新建标签页
browser_tab_new(url="https://github.com")

# 列出所有标签页
browser_tab_list()

# 切换到第二个标签页
browser_tab_switch(index=1)

# 关闭当前标签页
browser_tab_close()

# 关闭指定标签页
browser_tab_close(index=2)
```

## 下拉框与文件上传

```
# 选择下拉框选项
browser_snapshot()                              # 找到下拉框的 ref
browser_select(ref="@e3", values="option_value") # 单选
browser_select(ref="@e3", values=["v1", "v2"])   # 多选

# 上传文件
browser_upload(selector="input[type=file]", files="/path/to/file.pdf")
browser_upload(selector="#upload", files=["/path/a.pdf", "/path/b.pdf"])
```

## Cookie 管理

```
# 列出所有 cookies
browser_cookies(action="list")

# 设置 cookie
browser_cookies(action="set", name="token", value="abc123", domain="example.com")

# 清除所有 cookies
browser_cookies(action="clear")
```

## 三层认证架构

### Layer 1：持久化 Profile（默认，零配置）

浏览器使用持久化的 user data directory（`~/.xihe-agent/browser/profile/`），所有 cookie、localStorage、IndexedDB、service worker 跨重启保留。

**适用场景**：登录一次，后续自动免登录（90% 的场景）。

**工作方式**：
- `browser_navigate` 自动启动带持久化 profile 的浏览器
- 登录成功后，cookie 自动存入 profile 目录
- 下次启动浏览器时自动恢复登录态
- `browser_close` 不会删除 profile 数据

**无需任何配置**，这是默认行为。

### Layer 2：StorageState 导入导出

把登录态序列化为 JSON 文件，支持多身份切换。

**适用场景**：需要在不同账号间切换（工作号/个人号），或把登录态迁移到其他机器。

**工作流**：

```
# 1. 登录后保存状态
browser_state_save(name="github")

# 2. 切换到另一个账号
browser_navigate(url="https://github.com/login")
browser_type(@e1, "another@email.com")
browser_click(@e2)
browser_state_save(name="github-personal")

# 3. 加载之前的状态
browser_state_load(name="github")        # 切回工作号
browser_state_load(name="github-personal") # 切回个人号

# 4. 管理状态
browser_state_list()                      # 查看所有已保存状态
browser_state_delete(name="github-personal")  # 删除
```

**存储位置**：`~/.xihe-agent/browser/states/` 目录，每个状态一个 JSON 文件。

**注意**：
- `browser_state_load` 会关闭当前浏览器并重新启动
- StorageState 包含 cookies + localStorage，不含 IndexedDB/service worker
- 加载 state 后浏览器使用非持久化模式，如需切回持久化模式用 `browser_close` 后重新 `browser_navigate`

### Layer 3：CDP 连接用户浏览器

直接连接到用户正在运行的 Chrome/Edge，复用全部登录态。

**适用场景**：需要 2FA/短信验证码的网站、用户已在自己浏览器中登录的场景。

**工作流**：

```
# 1. 用户先启动带调试端口的浏览器
#    Windows: chrome.exe --remote-debugging-port=9222
#    macOS:   /Applications/Google\ Chrome.app/.../Chrome --remote-debugging-port=9222
#    Edge:    msedge.exe --remote-debugging-port=9222

# 2. 连接到用户的浏览器
browser_connect(url="http://localhost:9222")

# 3. 直接操作用户已登录的页面
browser_snapshot()
browser_click(@e5)
```

**注意**：
- 连接后，浏览器的所有页面都可访问
- CDP 模式下 `browser_close` 只断开连接，不关闭用户的浏览器
- 需要用户手动启动浏览器的调试端口

### 三层如何配合

```
用户请求访问需要登录的网站
       │
       ▼
  持久化 profile 有效？ ── 是 ──→ 直接访问（90% 场景止步于此）
       │ 否
       ▼
  有保存的 storageState？ ── 是 ──→ browser_state_load → 访问
       │ 否
       ▼
  用户浏览器正在运行？ ── 是 ──→ browser_connect → 复用登录态
       │ 否
       ▼
  agent 自助登录（填表单 + 等 2FA）→ 登录成功后
       │                    自动 browser_state_save + 写入持久化 profile
       ▼
  下次自动免登录
```

## Accessibility Tree 与 ref ID

`browser_snapshot` 返回页面的 accessibility tree，每个可交互元素带 ref ID：

```
heading 'GitHub' [@e1]
link 'Sign in' [@e2]
textbox 'Username' [@e3]
textbox 'Password' [@e4]
button 'Sign in' [@e5]
link 'Forgot password?' [@e6]
combobox 'Country' [@e7]
checkbox 'Remember me' [@e8]
```

使用 ref ID 交互最精准：

```
browser_click(ref="@e5")        # 点击 "Sign in" 按钮
browser_type(ref="@e3", text="user@email.com")  # 输入用户名
browser_hover(ref="@e7")        # 悬停下拉框
browser_select(ref="@e7", values="CN")  # 选择选项
browser_check(ref="@e8")        # 勾选复选框
```

也可以用 CSS selector 或文本作为 fallback：

```
browser_click(selector="#login-button")
browser_click(text="Sign in")
```

## 文件结构

```
~/.xihe-agent/browser/
├── profile/              # Layer 1: 持久化浏览器 profile
│   ├── Default/
│   │   ├── Cookies
│   │   ├── Local Storage/
│   │   └── ...
│   └── ...
└── states/               # Layer 2: 保存的登录态
    ├── github.json
    ├── work-google.json
    └── default.json
```

## 依赖安装

```bash
pip install playwright
playwright install chromium
```

如果没有 Playwright，browser_navigate 会降级为 httpx 抓取（只读，无交互能力）。

## 常见问题

### Q: 页面还没加载完就操作失败了？

使用 `browser_wait` 等待条件满足：
```
browser_wait(load_state="networkidle")
browser_wait(text="Dashboard")
browser_wait(selector=".loaded")
```

### Q: 下拉菜单点不开？

用 `browser_hover` 先悬停触发菜单，再点击子项：
```
browser_hover(ref="@e3")    # 悬停打开菜单
browser_snapshot()          # 重新获取菜单项的 ref
browser_click(ref="@e7")    # 点击菜单项
```

### Q: 登录态丢失了怎么办？

检查 `~/.xihe-agent/browser/profile/` 目录是否存在且未被删除。如果丢失了：
1. 用 `browser_state_load` 恢复之前保存的状态
2. 重新登录，登录后建议 `browser_state_save` 备份

### Q: 多个网站需要不同账号怎么办？

用 `browser_state_save` 为每个账号保存状态，需要时用 `browser_state_load` 切换。

### Q: 2FA/短信验证码怎么办？

1. 用 `browser_connect` 连接到你已登录的浏览器
2. 或在 agent 登录流程中，遇到验证码时通过 clarify 工具让用户告诉验证码

### Q: CDP 连接安全吗？

CDP 只监听 localhost，外部无法访问。但连接后拥有浏览器的完全控制权，请只在可信环境使用，用完后断开连接。

### Q: 怎么操作 iframe 里的元素？

```
browser_frame(selector="iframe")   # 切换到 iframe
browser_snapshot()                  # 操作 iframe 内容
browser_frame(action="main")       # 切回主框架
```
