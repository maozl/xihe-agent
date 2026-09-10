# 会话数据模型：sessions / messages 表与 reset 机制

> 核心文件：`src/core/session.py`（SessionDB / ResetPolicy / SessionSource）
> 消费方：`src/core/agent/agent.py`（持久化）、`src/gateway/serve/chat.py`（REST/WS 读写）、桌面端（只消费 serve API）

## 1. 表结构

### sessions 表（1 行 = 1 个对话）

```sql
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,  -- 确定性查找键：agent:main:{platform}:{chat_type}:{chat_id}
    session_id  TEXT NOT NULL,     -- 内部唯一 ID（messages 表外键），格式 {时间戳}_{uuid8}
    platform    TEXT,              -- serve / wecom / feishu / cron / delegate
    chat_id     TEXT,              -- 外部会话标识（桌面的 conv_id / 企微 chat_id）
    user_id     TEXT,
    chat_type   TEXT DEFAULT 'dm',
    title       TEXT,
    origin      TEXT,              -- SessionSource 序列化（重建会话用）
    was_auto_reset INTEGER DEFAULT 0,
    auto_reset_reason TEXT,        -- "idle" / "daily"
    model       TEXT,              -- per-session model override（/model 切换）
    created_at  TEXT,
    updated_at  TEXT
)
```

### messages 表（N 行 = 1 个对话的消息流）

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,      -- FK → sessions.session_id
    role TEXT NOT NULL,            -- user / assistant / system / tool
    content TEXT,                  -- 文本内容（assistant 的 tool_calls 行可为 NULL）
    tool_calls TEXT,               -- JSON 数组，仅 assistant 行（模型决定调工具时）
    tool_call_id TEXT,             -- 仅 tool 行（对应 assistant.tool_calls[].id）
    reasoning TEXT,                -- 持久化的模型思考（仅显示，不进模型上下文）
    usage TEXT,                    -- 该轮 token 用量 JSON（最终 assistant 行）
    created_at TEXT,               -- 消息时间戳
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)
CREATE INDEX idx_messages_session ON messages(session_id)
-- 另有 messages_fts（FTS5 全文索引，session_search 用）
```

### 三个 ID 的分工

| 字段 | 谁认识 | 用途 |
|---|---|---|
| `session_key` | 内部代码 | `get_or_create_session()` 的唯一索引，从 SessionSource 确定性推导 |
| `session_id` | 仅 DB 内部 | `messages.session_id` 外键；`load_messages()` / `rewrite_messages()` 按 it 加载 |
| `chat_id` | 桌面/企微/飞书 | 外部世界对"这个对话"的标识；serve 收到 WS 帧的 conv_id 就是 it |

## 2. 会话生命周期

### 创建/获取（`get_or_create_session`）

```
入口：agent.chat() 或 serve 的 _handle_send
  │
  ├─ source = SessionSource(platform, chat_id, user_id, chat_type)
  ├─ session_key = build_key(source) → "agent:main:serve:dm:{chat_id}"
  │
  ├─ 缓存里有 entry？
  │   ├─ 是 → 检查 ResetPolicy.should_reset()
  │   │   ├─ 需要重置 → _create_session(was_auto_reset=True)
  │   │   └─ 不需要 → 更新 updated_at，返回既有 session_id
  │   └─ 否 → 查 DB；有 → 加载缓存；没有 → _create_session()
  │
  └─ _create_session() → INSERT sessions 行，生成新 session_id
```

### 消息持久化（agent loop）

```
agent.chat() 内部，每次迭代结束调 _persist_messages(session_id, messages)
  → rewrite_messages()：增量前缀匹配（对比上次写入的 content hash），
    只 DELETE 分歧行 + INSERT 尾部新行 → O(增量) 而非 O(全量)
```

一轮（turn）的消息序列（DB 中的行）：

```
id=101 role=user      "帮我查一下..."
id=102 role=assistant tool_calls=[search_files(...)]
id=103 role=tool      (搜索结果 JSON)
id=104 role=assistant tool_calls=[read_file(...)]
id=105 role=tool      (文件内容)
id=106 role=assistant "查到了，结果是..."（无 tool_calls = settled）
```

### 中断时的部分文本持久化

```
agent loop 检查点 2（API 返回后、messages.append 前）被中断：
  content = 模型已流式输出的部分文本
  → messages.append({"role":"assistant", "content": content + "\n\n[被中断，以上为部分输出]"})
  → _persist_messages()  ← 部分文本带标记落库，刷新不丢
```

桌面端 `_reshape_history` 折叠时：
- 识别 `[被中断` 标记 → 不设 settled → `incomplete=True` → 显示"未完成"徽标 + "继续"按钮
- 显示层从 content 中剥掉标记（桌面看到干净的文本）
- 模型下一轮看到标记，知道该从断点续写

## 3. Reset 机制

### ResetPolicy（`session.py:136-165`）

```python
mode: "idle" | "daily" | "both" | "none"   # 默认 "idle"
idle_minutes: 1440                           # 默认 24 小时
daily_reset_hour: 4                          # 默认凌晨 4 点
```

配置（config.yaml `session` 段）：

```yaml
session:
  auto_reset_mode: idle
  idle_minutes: 1440
  daily_reset_hour: 4
```

### 触发时机

`get_or_create_session()` 每次被调用时检查：

- **idle 模式**：`updated_at + idle_minutes < now` → 触发
- **daily 模式**：`updated_at < 今天{daily_reset_hour}点` → 触发
- **both**：任一条件满足即触发

### 重置做了什么

```
_reset（自动 or 手动）:
  旧 session_id 的 messages 行不动（留在 DB，但不被引用）
  sessions 表同一 session_key 换成新 session_id
  was_auto_reset=1, auto_reset_reason="idle"/"daily"
  → 下一轮模型从空白开始
```

### 手动 reset vs 删除

| 操作 | API | 效果 |
|---|---|---|
| 手动 reset | `POST /convs/{id}/reset` | 同一 session_key 换新 session_id；旧消息留存但不可达 |
| 删除 | `DELETE /convs/{id}` | sessions 行 + 当前 session_id 的 messages 全删；FTS 索引同步 |

### reset 旧消息的生命周期

被 reset 替换掉的旧 session_id 对应的 messages 行：
- **不被自动清理**（delete_session 只删当前 session_id 的消息）
- 没有任何 sessions 行引用它们 → 成为孤儿行
- `session_search`（FTS 全文搜索）仍能搜到（FTS 索引不区分 session 是否活跃）
- 无害但会缓慢累积；如需清理可按 `messages.session_id NOT IN (SELECT session_id FROM sessions)` 扫

## 4. 数据流总图

```
桌面端                    serve                        SessionDB
──────                    ─────                        ─────────
会话列表 ←── GET /sessions ── list_sessions() ── SELECT sessions

选会话 → activeConvId
  │
发消息 → WS {"type":"send","conv_id":"..."}
  │         │
  │         ├─ _source(conv_id) → SessionSource
  │         ├─ build_key() → session_key
  │         ├─ get_or_create_session() ←── ResetPolicy 检查在这里
  │         ├─ create_agent() → agent.chat(source, text)
  │         │     │
  │         │     ├─ load_messages(session_id)  ← 加载历史
  │         │     ├─ 修复悬空 tool_calls + 注入恢复提示
  │         │     ├─ agent loop（迭代）
  │         │     │   ├─ API 调用 → assistant_msg append
  │         │     │   ├─ 工具执行 → tool_msg append
  │         │     │   └─ _persist_messages() ←── 每次迭代落库
  │         │     └─ return 最终文本
  │         │
  │         └─ complete 事件（WS 推送）
  │              content = 最终回复文本（唯一权威源）
  │
查看历史 ←── GET /convs/{id}/messages ── _reshape_history()
                                               │ 折叠消息流为气泡
                                               │ user 行 → user 气泡
                                               │ assistant+tool 行 → assistant 气泡
                                               │    tools = tool 行计数
                                               │    has_reasoning = reasoning 非空
                                               │    incomplete = 无 settled 行
                                               │    ts = 首行 created_at
                                               └ 返回 [{role, content, id, tools, ...}]

展开 trace ←── GET /convs/{id}/trace/{msg_id} ── 从 anchor 行走到下一条 user 行
                                                   收集 tool_calls + reasoning 事件
```

## 5. 关键不变量

1. **session_key 是确定性的**：同一个 (platform, chat_id, user_id, chat_type) 永远生成同一个 key
2. **messages 只增不删**（除非 reset/delete）——正常迭代中 `rewrite_messages` 是前缀追加
3. **agent loop 每次迭代后落库**——中断/崩溃不丢已完成的工作
4. **部分中断文本带标记落库**（2026-09-10 加）——刷新后保留 + fold 识别为未完成
5. **reset 换 session_id 不删旧消息**——旧消息在 DB 但不可达
6. **`_reshape_history` 的 settled 判据**：无 tool_calls 的 assistant 行 = 最终回复；带 `[被中断` 标记的除外
