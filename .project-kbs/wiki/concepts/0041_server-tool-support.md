---
type: concept
title: Server Tool（服务端内置工具）——调研与 xihe 支持路径
slug: 0041_server-tool-support
aliases:
  - server tool
  - 服务端内置工具
  - hosted tools
  - 内置工具
tags:
  - llm-api
  - tools
  - research
status: active
created: 2026-08-31
updated: 2026-09-01
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0034_three-layer-agent-roster.md
  - wiki/concepts/0040_external-agent-adapter-protocol.md
  - wiki/concepts/0024_desktop-serve-protocol.md
---

# Server Tool（服务端内置工具）——调研与 xihe 支持路径

> 设计参考（未实施）。2026-08-31 调研 + 内部网关探针实测。

## 摘要

**server tool** = 在请求 `tools` 里声明**非 function 条目**（如 `{"type": "web_search"}`）、由**模型提供方在服务端执行**的工具。客户端不执行、通常也不需要回传结果——与 xihe 现有全部工具（client tool：模型发 `tool_calls` → 本地 `registry.dispatch` → 回传 `role:tool` 消息）相对。

定位结论：**对话增强类能力（联网搜索/代码沙箱）的方向已定**（Anthropic/OpenAI/Gemini/智谱四家全部 GA + 按次计费 + 能力持续从客户端往服务端搬），但对 xihe 是「**透传能力声明**」级别的补充通道，**不是工具系统的替代**——agent 作业（读写本地文件、操作内网系统）结构性留在客户端，服务端碰不到内网与凭据。

## 三家形态

| 提供方 | 声明形状（tools 条目） | 结果如何回来 | 计费 |
|---|---|---|---|
| Anthropic Messages | `{"type": "web_search_20250305", "name": "web_search"}`（还有 code_execution / computer / bash / text_editor / MCP connector，类型带日期版本号） | 响应 content blocks：`server_tool_use` + `web_search_tool_result` | 按次 |
| OpenAI（仅 Responses API） | `{"type": "web_search"}` / `code_interpreter` / `file_search` / `mcp` | 输出 items：`web_search_call` 等 | 按次 |
| 智谱 chat.completions | `{"type": "web_search", "web_search": {enable, search_engine, count, search_prompt…}}` | **inline 风格**：正文 `[来源: ref_N]` 引用 + 顶层 `web_search` 数组，无 tool_calls | 0.01–0.05 元/次 |

两种响应风格要区分：**inline 透明**（智谱：结果融进生成，循环零改动）vs **显式块**（Anthropic/OpenAI：server 执行以块/item 形式出现在输出里，历史回传要处理）。

## 内部网关实测矩阵（2026-08-31）

直连 `<内部网关IP>/public/v1` 打探针（脚本：OS temp `probe_server_tool*.py`）：

| 路径 | 模型组 | tools 形状 | 结果 |
|---|---|---|---|
| chat.completions | **glm-5.2-zp-openai（现配组）** | 智谱 nested（enable 布尔/字符串/带 engine/仅 engine/空对象 共 6 变体） | ⚠️ **200 但全部空转**——无搜索、模型自述无能力、无顶层 web_search 字段 |
| chat.completions | glm-5.2-zp-openai | `{"type":"web_search","name":…}`（缺子对象） | 400「tools[0].web_search 不能为空」→ **上游认识该类型**（litellm 原样转发，声明层支持） |
| chat.completions | glm-5.2-zp-openai | Anthropic 版本化类型 | 400「type is illegal」（上游拒） |
| chat.completions | glm-5.2-zp | 智谱 nested | 500（litellm 走 AnthropicConfig 转换，报缺 name） |
| chat.completions | glm-5.2-zp | **`{"type":"web_search_20250305","name":"web_search"}`** | ✅ **真执行**：当天真实新闻 + 引用 + **信息性 tool_call** |
| responses | glm-5.2-zp / -openai | `{"type":"web_search"}` | ✅ 真执行（output 含 web_search_prime function_call item）/ 404（上游无 /responses） |

关键推论：

1. **`glm-5.2-zp` 组 = litellm 的 Anthropic 协议通道**（错误栈露出 AnthropicConfig；这也解释了 codex 走 Responses API 可用，见 [[0040]]）。chat.completions 上就能用 **Anthropic 形状**的 server tool——xihe 不需要换协议。
2. **信息性 tool_call**：`glm-5.2-zp` 上 server 执行完成后，响应 `message.tool_calls` 里带一个 `name: "web_search_prime"` 的 function 条目（参数 `location/search_query/search_recency_filter` 由后端生成，**客户端不可控**引擎/条数），正文即最终答案。这是 agent 循环必须识别的形态。
3. **二轮协议验证**：给信息性 tool_call 回一条合成 tool result（`"executed by provider"`）→ 协议接受，续聊正常（模型正确复述搜索所得）。**合成 result 策略成立**。
4. **现配 `-openai` 组 = 声明层认识、执行层空转、失败完全静默**（200 无报错）——最危险的失败模式；在该组上做任何支持都白配。可能是上游账号未开通搜索能力（智谱端点无权限时静默跳过）或部署未接线，从外部无法区分，需网关侧确认。
5. **server 端搜索能出公网**（探针搜到当天新闻）→ 对「内网部署」是数据出网边界，采用前需明确确认。
6. 附带发现：`glm-5.2-zp` 桥接会发 `thinking_blocks` + `reasoning_content`（与 active.md 桌面 thought_delta 待办相关——换组顺带解锁思考通道）。

## xihe 代码缺口（要动哪里）

| 位置 | 问题 |
|---|---|
| `src/core/agent/agent.py:1198` / `:1290` | tools 序列化硬取 `s["function"]`，非 function 条目直接 KeyError；`if tool_schemas:` 判空在纯 server tool 时漏发整个 tools |
| `core/agent/agent.py:937`/`:1011` dispatch + `core/registry.py:272` | 假设每个 tool_call 都归本地执行；未知名 → `tool_error("Unknown tool")`——信息性 tool_call 会被误执行并报错 |
| `agent.py:887-893` | assistant 消息重建硬编码 `"type": "function"` |
| `agent.py:553` `_repair_dangling_tool_calls`、`compressor.py:245` `_sanitize_tool_pairs` | 都假设「每个 tool_call 必有本地 tool 行」 |
| `session.py:306-319` | 持久化列仅 content/tool_calls/tool_call_id/reasoning/usage——响应顶层 `web_search` 数组等元数据会丢 |
| `config.py:221-226` | 新 config 键必须进**两个白名单循环**（已知陷阱，见 [[0034]]） |
| `serve.py:329-352` 流式协议 | `tool_call`/`tool_result` 成对假设；desktop `store.ts:420-436` 按名字 FIFO 配对，只有 start 无 result 会留卡死的 running 条目 |

## 方案设计（未实施）

核心原则：**server tool 不是工具注册表成员**——不进 ToolRegistry（无 handler、不可 dispatch）、不进 TOOLSETS、不进系统提示词（模型本来就认识自己的内置工具）。独立 config 键 + 原样透传（与 `request_extra` 同思路：三家声明形状互不兼容，抽象层是负资产）。

1. **config**：顶层 `server_tools:` 列表，条目原样进请求 `tools`（形状跟模型组走：Anthropic 通道→`web_search_20250305`；智谱直连→nested 形状）。不写/`[]` = 关（与 roster 三态语义对齐）。须加进 config.py 两个白名单循环。
2. **请求**：`agent.py` 两处序列化改 `tools = [{"type":"function","function":…}…] + self.server_tools`，判空条件改 `if tools:`。
3. **响应（核心技巧）**：tool_call 名字命中 server 名单（config 声明的 name + 别名表如 `web_search→web_search_prime`）→ **不 dispatch、不计迭代**，直接合成：
   ```python
   messages.append({"role": "tool", "tool_call_id": tc["id"],
                    "content": json.dumps({"note": "executed by provider"})})
   ```
   这一招让 dangling repair / compressor sanitize / 「每个 tool_call 必有 tool 行」不变式**全部不用改**，且二轮探针已证协议接受。未命中名单的未知名 tool_call 仍走现有 `Unknown tool` 报错路径（保留真 bug 的信号）。
4. **流式零协议改动**：信息性 tool_call 走现有 `tool_call` 事件、合成 result 走 `tool_result` 事件 → desktop 现有 trace 渲染零改动即可显示一条「web_search（已由模型服务执行）」。
5. **边界要写清**：不经本地审批门（dispatch 管不着 server tool）、按次计费、默认关；可选启动时 capability probe 防「配错形状静默无效」。
6. **测试**：L2 层 `FakeChatClient` 剧本返回带 `web_search_prime` tool_call 的响应，断言不 dispatch、合成 result 形状、二轮消息合法。

## 方向判断（两车道）

| | 车道 A：对话增强 | 车道 B：agent 作业 |
|---|---|---|
| 典型能力 | 联网搜索、代码沙箱、文件检索 | 读写本地文件、操作内网系统、CI、CMDB |
| 归属 | **server tool 是终局**，基本商品化 | **client tool + MCP 主导**，结构性留在客户端 |
| 证据 | ChatGPT search、Gemini grounding | Claude Code / Codex CLI / Cursor 全是本地执行 |

对 xihe：**低成本补强，非路线调整**。注意 xihe 已有客户端搜索工具（`web_tools.py`，Tavily/SerpAPI，`check_fn` 门控）——server tool 的真实增量 = 免本地配搜索 API key + 引用与生成融合；代价 = 失去审批/控制/成本护栏。

## 开放问题

1. **模型组决策（前置）**：切 `glm-5.2-zp`（需评估模型名变化影响；顺带解锁 thinking 通道）或推动网关给 `-openai` 组接搜索（顺带把静默跳过改显式报错）。
2. **公网数据边界**：server search 出公网是否符合内网部署预期，需用户/安全侧确认。
3. **流式行为未测**：`stream=true` 时信息性 tool_call 的到达形态需补探针。
4. **计费护栏**：按次计费，xihe 侧无 per-turn 成本上限（可后续 config 加 max_calls）。

## 相关页面

- [[0002_tool-registry-and-dispatch]] - client tool 的注册与调用链（本页的对照面）
- [[0034_three-layer-agent-roster]] - roster 三态语义与 config 白名单两循环陷阱
- [[0040_external-agent-adapter-protocol]] - 同一内部网关的 Responses API 通道（codex 实测）
- [[0024_desktop-serve-protocol]] - WS 事件协议（tool_call/tool_result 事件复用点）
