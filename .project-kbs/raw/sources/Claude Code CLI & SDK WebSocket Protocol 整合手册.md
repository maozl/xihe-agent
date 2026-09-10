# Claude Code CLI \& SDK WebSocket Protocol 整合手册

# ClaudeCode\_CLI\_SDK\\\[\_Manual\.md\]\(\_Manual\.md\)

> 文档分区清晰分离：【官方 CLI\&SDK 协议】｜【Halo hello\-halo 专属实践指南】
> 可直接全选复制保存为 UTF\-8 Markdown 文件
> 
> 

```markdown
# Claude Code CLI & SDK WebSocket Protocol 整合手册
## 文档信息
来源：Anthropic official docs
页面：
https://code.claude.com/docs/en/cli-reference
https://code.claude.com/docs/en/sdk
适用场景：第三方桌面Agent、进程托管、`--sdk-url` WebSocket程序化接入Claude CLI
通信标准：NDJSON（换行分隔JSON）

> ⚠️ 分区说明
> 第一、第二部分：纯粹 Anthropic Claude Code 官方规范（不包含 Halo）
> 第三部分：独立章节 — hello-halo(Halo) 落地实践、Runtime配置、架构方案、踩坑方案

# 第一部分 CLI 完整启动参数参考
## 1.1 会话与进程隔离核心参数
| 参数 | 说明 | 使用示例 |
| ---- | ---- | ---- |
| --sdk-url <ws-url> | 启用SDK WebSocket通信模式 | --sdk-url ws://127.0.0.1:58900 |
| --worktree <path> | 独立隔离工作目录，多会话环境隔离 | --worktree ./sessions/xxx-uuid |
| --session-id <uuid> | 手动指定会话唯一ID | --session-id 8832af72-xxxx |
| --resume <session-id> | 加载并恢复已持久化会话 | --resume sess_001 |
| --fork-session | 在现有会话基础上克隆全新会话，原会话不受影响 | --resume sess_001 --fork-session |
| --name <string> | 设置会话展示名称 | --name "后端重构任务" |
| --model <name> | 指定使用模型 | --model claude-opus-4-6 |
| --effort low/medium/high/max | 设置推理深度 | --effort max |

## 1.2 权限管控参数（防止进程阻塞等待y/n交互）
权限模式主参数
```

\-\-permission\-mode \[plan / acceptEdits / auto / dontAsk / manual / bypassPermissions\]

```Plain Text
模式释义：
- plan：只读模式；禁止文件修改、禁止bash执行，仅代码调研（最高安全）
- acceptEdits：允许文件写入，执行外部命令需要人工审批
- auto：自动执行可信操作，内置安全校验
- dontAsk：仅白名单工具自动执行，非白名单直接拒绝
- bypassPermissions：高危，跳过全部交互确认，自动化后台场景专用

附属权限参数
- --allowedTools "Read,Glob,Grep" 工具白名单
- --disallowedTools "Bash,Edit" 工具黑名单
- --dangerously-skip-permissions 完全关闭交互式确认弹窗
- --add-dir D:/code/proj 允许访问指定目录
- --deny-dir C:/Windows 禁止访问指定系统目录

## 1.3 系统提示与项目规则注入
```bash
# 追加系统提示（推荐，保留内置Agent行为规则）
--append-system-prompt-file ./rules/project-spec.md
--append-system-prompt "所有代码必须附带单元测试"

# 完全覆盖系统提示（慎用，丢失原生内置规则）
--system-prompt "你是资深后端工程师，输出简洁Go代码"
```

## 1\.4 MCP 外部工具扩展

```bash
# 加载MCP配置文件
--mcp-config ./mcp-servers.json

# 强制仅使用配置内MCP，不加载全局用户MCP服务
--strict-mcp-config --mcp-config ./mcp-servers.json
```

## 1\.5 调试与运行控制参数

- \-\-verbose 输出完整 NDJSON 消息日志，调试必备

- \-\-no\-session\-persistence 关闭会话持久化，进程销毁上下文直接丢失

- \-\-safe\-mode 纯净模式：禁用 \[CLAUDE\.md\]\(CLAUDE\.md\)、MCP、插件、钩子

- \-\-bare 最小脚本模式，关闭 LSP 同步、扩展加载

- \-\-disable\-slash\-commands 禁用全部内置斜杠指令

- \-\-include\-partial\-messages 下发流式增量片段，支持前端实时渲染输出

## 1\.6 顶层子命令（终端直接执行）

```bash
claude doctor                 # 环境自检、故障诊断
claude auth login/status/logout
claude stop <session-id>      # 终止后台运行会话
claude respawn <session-id>   # 重启会话，保留上下文
claude rm <session-id>        # 删除会话持久化记录
claude setup-token            # 创建长期CI授权token
claude mcp                    # MCP服务管理命令
claude remote-control         # 开启远程控制服务
```

## 1\.7 会话内斜杠指令（SDK 消息下发即可触发）

```Plain Text
/help                    查看帮助
/clear                   清空当前对话上下文
/compact [自定义提示]    压缩会话上下文，控制Token占用
/save                    持久化会话
/exit                    优雅退出Agent进程
/plan                    切换只读调研模式
/status                  查询会话状态、权限配置
/cost                    查看Token消耗估算
/effort low|medium|high|max
/list-mcp-servers        列出当前加载的MCP服务
/init                    在目录自动生成 CLAUDE.md 项目规则文件
```

> 开发规范：禁止直接 kill 进程强制关闭会话；
> 标准流程：下发 `/save` → 下发 `/exit` → 等待进程正常退出 → 超时阈值到达后执行强制 Kill。
> 
> 

# 第二部分 SDK WebSocket 通信协议规范

## 2\.1 基础传输规则

1. 传输协议：WebSocket ws://（本地内网，无需 wss 加密）

2. 消息编码：UTF\-8 JSON；每条消息以换行符 `\n` 结尾（NDJSON 标准）

3. 数据流双向：

    - Client \(上层 GUI\) → claude：用户提问、权限审批、任务中断指令

    - claude → Client：文本流输出、工具调用、权限申请、系统事件

## 2\.2 标准消息结构示例

### 2\.2\.1 初始化事件（claude 连接成功后第一条推送）

```json
{
  "type": "system",
  "subtype": "init",
  "session_id": "sess-uuid",
  "uuid": "消息唯一id",
  "cwd": "/工作目录路径",
  "model": "claude-sonnet-4-6",
  "claude_code_version": "2.1.210",
  "tools": ["Read","Write","Edit","Bash","Glob","Grep"],
  "mcp_servers": [{"name":"xxx","status":"connected"}]
}
```

### 2\.2\.2 下发用户提问消息

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "实现用户登录接口并编写单元测试"
  }
}
```

### 2\.2\.3 Claude 流式增量输出消息

多条分片拼接成完整回复

```json
{
  "type": "stream_event",
  "delta": {"type":"text_delta","text":"函数代码开始：\n"}
}
```

### 2\.2\.4 权限申请请求

Agent 执行修改文件 / Shell 命令时触发

```json
{
  "type":"control_request",
  "request_id":"req-xxx",
  "request":{
    "subtype":"permission_request",
    "action":"edit_file",
    "target":"src/main.go"
  }
}
```

审批响应报文

```json
{
  "type":"control_response",
  "request_id":"req-xxx",
  "response":{"allow":true}
}
```

allow:true = 允许，allow:false = 拒绝

### 2\.2\.5 中断当前运行任务

```json
{
  "type":"control_request",
  "request": {"subtype":"interrupt"}
}
```

### 2\.2\.6 任务执行结束通知

```json
{
  "type":"result",
  "success":true,
  "usage": {"input_tokens":1200,"output_tokens":850}
}
```

## 2\.3 STDIO 模式 vs WebSocket \(\-\-sdk\-url\) 对比

1. STDIO 模式

    - 使用管道 stdin/stdout 通信，无需额外搭建 ws 服务

    - Windows 环境极易出现终端 IO 阻塞、权限交互卡死

2. WebSocket 模式【第三方桌面工具推荐】

    - 规避 Windows ConPTY 终端各类阻塞 Bug

    - 连接状态可监控，方便实现心跳保活

    - 消息边界稳定，缓冲区问题更少

## 2\.4 协议开发避坑清单

1. 每条 JSON 消息末尾必须携带 `\n`，否则 claude 无法解析多条消息；

2. 权限请求必须及时应答，长时间无响应会导致 Agent 进程挂起；

3. WebSocket 断开后 claude 不会自动重连，上层需要监控连接状态，重启子进程；

4. 禁止单进程并发多条任务，多对话场景必须启动多个独立 claude 进程；

5. 大批量消息发送建议增加简单限流，避免消息丢失。

# 第三部分 hello\-halo（Halo）独立实践指南

> 本章节仅针对 openkursar/hello\-halo，与上面 Claude 官方参数解耦
> 适用目标：多 Agent 运行、Claude/Codex/CodeBuddy 混合调度、进程托管二次开发
> 
> 

## 3\.1 Halo 底层架构简述

1. 基于 Tauri \(Rust\) \+ 前端 GUI；

2. 内置 Runtime 抽象层：ClaudeRuntime / CodexRuntime / 自定义 StdioRuntime；

3. 底层机制：每一个会话 = 独立子进程 spawn 托管；

4. 通信默认支持两种方式：STDIO NDJSON / `--sdk-url` WebSocket；

5. 原生支持多工作空间隔离、会话持久化、MCP 服务管理。

## 3\.2 Halo 内 Claude Runtime 标准启动参数模板

直接填入 Halo 配置面板 / 运行时构造参数

```bash
claude
--sdk-url ws://127.0.0.1:{WS_PORT}
--worktree {SESSION_WORK_DIR}
--session-id {SESSION_UUID}
--name "{SESSION_DISPLAY_NAME}"
--model claude-opus-4-6
--effort high
--permission-mode plan
--append-system-prompt-file ./.agent-rules.md
--verbose
--include-partial-messages
```

### 参数适配 Halo 的最佳实践

1. Windows 平台强制启用 `--sdk-url` WebSocket 模式，不要使用 STDIO；

2. `--worktree` 映射到 Halo 会话持久化目录，实现会话隔离；

3. 新建会话自动生成独立 uuid 作为 `session-id`；

4. 权限模式默认使用 `plan`，用户需要编辑代码再切换 `acceptEdits`。

## 3\.3 Halo 中多 Agent 混合接入方案（Claude \+ Codex \+ CodeBuddy）

### 方案 A：顶层多会话并行（开箱即用，无需修改源码）

1. 在 Halo 新建多个会话；

2. 会话 1 绑定 ClaudeRuntime；

3. 会话 2 绑定 CodexRuntime；

4. 会话 3 配置自定义 StdioRuntime 接入 CodeBuddy CLI；
优势：零代码修改，适合横向对比不同 Agent 输出。

### 方案 B：MCP 互通方案（推荐二次开发）

在 Halo 中为 Claude 会话加载 MCP 配置，把 Codex、CodeBuddy 注册为可调用工具
`mcp-servers.json`

```json
{
  "mcpServers": {
    "codex-agent": {
      "command": "codex",
      "args": ["--stdio"]
    },
    "codebuddy": {
      "url": "http://127.0.0.1:8090/mcp",
      "type": "http"
    }
  }
}
```

加载方式：启动 claude 附加参数 `--mcp-config ./mcp-servers.json`

> 效果：在同一个 Claude 对话内，直接下发指令调用 Codex/CodeBuddy 执行子任务。
> 
> 

### 方案 C：Halo 上层编排流水线（深度改造）

在 Runtime 上层增加调度器，实现任务自动流转：
任务 → Claude 架构分析 → 产出文档 → 自动投递 Codex 编码 → CodeBuddy 自动化测试

## 3\.4 Halo 高阶玩法清单

1. **会话 Fork 克隆**
在 Halo 选中已有会话，使用 `--fork-session` 派生新会话，主干上下文不受污染，并行开展多个子任务。

2. **多套权限策略模板**
在 Halo 前端预设按钮：调研模式 / 开发模式 / 全自动模式，一键切换 `--permission-mode`。

3. **工作空间自动注入 \[CLAUDE\.md\]\(CLAUDE\.md\)**
Halo 新建项目目录时自动生成规则文件，通过 `--append-system-prompt-file` 加载。

4. **进程看门狗托管**
利用 Tauri 进程监控：检测卡死、失联进程，自动执行优雅退出 \+ 重启；父进程关闭批量回收全部子进程，杜绝僵尸进程。

5. **会话关闭标准链路（Halo 必须实现）**
GUI 关闭会话 → WebSocket 下发 `/save` → 下发 `/exit` → 等待正常退出（3s 超时）→ SIGTERM → 最后兜底 SIGKILL。

## 3\.5 Halo 专属踩坑清单

1. ❌ 不要共用同一个 claude 进程承载多个会话
Halo 每个 Tab 会话必须独立 spawn；claude CLI 原生不支持多并发任务。

2. ❌ Windows 环境不要裸 STDIO 运行
极易卡在权限确认 y/n 等待，进程假死；强制启用 sdk\-url websocket。

3. ❌ MCP 服务不要一次性挂载过多
会占用上下文窗口，建议按任务动态启用、动态卸载 MCP 服务。

4. ❌ 不要频繁强制 kill \-9
未执行 `/save` 会丢失内存对话上下文。

5. ⚠️ CodeBuddy 限制
若无官方 stdio/NDJSON 协议，则无法直接作为 Runtime 接入；需要部署 LiteLLM 网关做协议转换。

## 3\.6 Halo 二次开发扩展点

1. 扩展新 Runtime：复制 ClaudeRuntime 结构体，实现统一 Agent Trait，接入 CodeBuddy 等第三方 Agent；

2. 前端新增 MCP 服务可视化管理面板，运行时动态增删 MCP；

3. 增加 Token 用量统计面板，解析每条消息 usage 数据；

4. 增加会话导出 / 导入功能，持久化整套对话与工作空间配置。

# 备查官方链接

CLI Reference：[https://code\.claude\.com/docs/en/cli\-reference](https://code.claude.com/docs/en/cli-reference)
SDK 文档：[https://code\.claude\.com/docs/en/sdk](https://code.claude.com/docs/en/sdk)
MCP 文档：[https://code\.claude\.com/docs/en/mcp](https://code.claude.com/docs/en/mcp)

```Plain Text
## 使用说明
1. 全选以上文本复制；
2. 新建文本文档 → 粘贴；
3. 另存为，编码 UTF-8，文件名：`ClaudeCode_Halo_Integrated_Manual.md`

如果你后续需要，我可以再单独输出一份【精简版Halo开发Checklist】，方便你调试Runtime。
```

```Plain Text

```

> （注：部分内容可能由 AI 生成）
