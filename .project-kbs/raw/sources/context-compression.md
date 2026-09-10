# 上下文压缩

## 概述

当对话 token 数接近模型上下文窗口阈值时，自动压缩历史对话，释放空间给新的交互。

## 触发条件

估算 token 数 ≥ `context_length × threshold_percent`（默认 50%）

例如：context_length=128000, threshold=0.50 → 阈值 64000 tokens

## 压缩算法（4 步）

### Step 1: 裁剪旧 tool 结果

把远处的 tool 输出（>200 字符）替换为 `[Old tool output cleared to save context space]`。

保护最近 20 条消息中的 tool 结果不裁剪。

**目的**：tool 输出通常最长（文件内容、搜索结果），裁剪后大幅减少后续摘要的 token 消耗。

### Step 2: 划分三段

```
[head]  [middle → 待压缩]  [tail]
  3条     变长               ~20K tokens
```

- **head**（前 3 条）：system prompt + 首轮对话，始终保留
- **tail**（最近 ~20K tokens）：当前上下文，始终保留
- **middle**（中间段）：被摘要替换

### Step 3: LLM 摘要 middle

将 middle 段的完整对话（用户消息 + assistant 回复 + tool 调用 + tool 结果）交给 auxiliary LLM 生成结构化摘要。

**首次压缩**：
```
Create a structured handoff summary for a later assistant.

TURNS TO SUMMARIZE:
[USER]: 帮我分析项目结构
[ASSISTANT]: 我来查看目录...
[TOOL RESULT]: {"tree": "..."}
...

Use this structure:
## Goal
## Progress (Done / In Progress / Blocked)
## Key Decisions
## Relevant Files
## Next Steps
```

**增量更新**（已有上一次摘要）：
```
Update the context compaction summary below by incorporating new turns.

PREVIOUS SUMMARY:
## Goal: ...

NEW TURNS:
[USER]: 继续分析
...

Use the same structure. Be specific — include file paths, commands, error messages.
```

摘要目标 ~2000 tokens，由 auxiliary client 调用（可用更便宜的模型）。

### Step 4: 拼装 + 修复

```
[head] + [摘要消息] + [tail] = 压缩后的 messages
```

**摘要消息的 role 选择**：根据前后消息的 role 避免连续相同 role（如 assistant 后不能接 assistant）。

**修复孤立 tool_call/tool_result 对**：
- 压缩后可能残留没有对应 result 的 tool_call → 补 stub result
- 压缩后可能残留没有对应 tool_call 的 tool result → 删除

## token 估算

```
估算 tokens = Σ (len(content) / 4 + 10) + Σ tool_call arguments / 4
```

4 字符约 1 token，每条消息加 10 token 开销。这是粗估，不做精确 tiktoken 计算。

## 关键代码路径

| 函数 | 作用 |
|------|------|
| `should_compress(messages)` | 估算 token 数，判断是否需要压缩 |
| `compress(messages)` | 执行 4 步压缩，返回新的 messages 列表 |
| `_prune_old_tool_results(messages)` | Step 1：裁剪旧 tool 输出 |
| `_find_tail_start(messages, head_end)` | Step 2：从后往前走，保护 ~20K tokens |
| `_generate_summary(turns)` | Step 3：LLM 生成摘要 |
| `_sanitize_tool_pairs(messages)` | Step 4：修复孤立 tool call/result |

## 调用点

`core/agent.py` 的 `_chat_step()` 方法中：

```python
# 每次 API 调用前检查
if self.compressor.should_compress(messages):
    messages = self.compressor.compress(messages)
```

## 涉及文件

| 文件 | 作用 |
|------|------|
| `core/compressor.py` | 压缩算法实现 |
| `core/agent.py` | 调用入口（`_chat_step`） |
| `core/auxiliary_client.py` | 摘要 LLM 调用 |
