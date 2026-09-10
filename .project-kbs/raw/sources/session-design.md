# Xihe-Agent Session Design

## Core Concepts

### Two-Layer ID

| ID | Role | Format | Example |
|---|---|---|---|
| `session_key` | Deterministic logical key: "which conversation" | `agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}][:{user_id}]` | `agent:main:wecom:dm:chat123` |
| `session_id` | Physical instance: "which round" | `{YYYYMMDD_HHMMSS}_{uuid8}` | `20260522_143052_a1b2c3d4` |

- `session_key` is computed from message source, never changes for the same conversation
- `session_id` changes when a session is reset (idle timeout, daily reset, manual /new)
- All messages are stored under `session_id`; `session_key` maps to the current active `session_id`

### Session Key Generation

`build_session_key(source)` generates a deterministic key from the message origin:

**DM rules:**
- With chat_id + thread_id: `agent:main:{platform}:dm:{chat_id}:{thread_id}`
- With chat_id only: `agent:main:{platform}:dm:{chat_id}`
- With thread_id only: `agent:main:{platform}:dm:{thread_id}`
- Neither: `agent:main:{platform}:dm`

**Group/channel rules:**
- Base: `agent:main:{platform}:{chat_type}:{chat_id}`
- With thread_id: `agent:main:{platform}:{chat_type}:{chat_id}:{thread_id}`
- With user_id (group isolation): append `:{user_id}`
- Threads default to shared sessions (all participants share one conversation)

### chat_type

| Type | Meaning | Session Isolation |
|---|---|---|
| `dm` | Direct message / private chat | Per chat_id (and optionally thread_id) |
| `group` | Group chat | Per chat_id + user_id (if group_sessions_per_user) |
| `channel` | Public channel | Per chat_id |
| `thread` | Thread/topic within a chat | Per thread_id, shared across users by default |

### thread_id

Sub-topic ID within a chat for finer session isolation. Use cases:
- Telegram forum topics (topic_id)
- Discord threads (thread_id / channel_id)
- Slack threads (thread_ts)

DM thread seeding: When a new thread session is created in a DM context, the parent DM session's history is automatically copied into it, so context carries over.

### SessionSource

Dataclass describing where a message comes from:

```python
@dataclass
class SessionSource:
    platform: str          # "wecom", "feishu", "telegram", etc.
    chat_id: str           # Chat/conversation ID
    chat_name: str = None  # Human-readable name
    chat_type: str = "dm"  # "dm", "group", "channel", "thread"
    user_id: str = None    # Sender user ID
    user_name: str = None  # Sender display name
    thread_id: str = None  # Sub-topic/thread ID
```

### SessionEntry

Tracks the current session for a session_key:

```python
@dataclass
class SessionEntry:
    session_key: str
    session_id: str
    created_at: datetime
    updated_at: datetime
    origin: SessionSource = None
    chat_type: str = "dm"
    was_auto_reset: bool = False
    auto_reset_reason: str = None  # "idle" or "daily"
```

## Reset Policies

Replace hardcoded 24h idle with configurable policies:

| Mode | Behavior |
|---|---|
| `idle` | Reset after N minutes of inactivity |
| `daily` | Reset at a specific hour each day |
| `both` | Reset on whichever condition triggers first |
| `none` | Never auto-reset |

Config example (in config.yaml):

```yaml
session:
  default_reset: idle
  idle_minutes: 1440     # 24 hours
  daily_reset_hour: 4    # 4 AM local time
  group_sessions_per_user: true
  thread_sessions_per_user: false

  # Per-platform overrides
  platforms:
    wecom:
      idle_minutes: 720  # 12 hours for WeCom
    feishu:
      reset: daily
      daily_reset_hour: 3
```

## API Design

### SessionDB.get_or_create_session(source: SessionSource) -> str

The only way to get/create a session. Takes `SessionSource`, internally generates the session_key via `build_session_key()`, returns `session_id`.

### XiheAgent.chat(source: SessionSource, user_message: str, ...) -> str

Agent receives a `SessionSource` instead of separate `session_key`/`platform`/`chat_id`/`user_id` args. All routing info is in the source.

### MessageEvent.to_session_source(platform: str) -> SessionSource

Platform adapters create a `MessageEvent` with `chat_type`, `thread_id`, etc. The gateway calls `to_session_source()` to build the `SessionSource` for `agent.chat()`.

### Special sources (cron, delegate, cli)

Internal callers create `SessionSource` directly:
- Cron: `SessionSource(platform="cron", chat_id="cron_{job_id}_{timestamp}", chat_type="dm")`
- Delegate: `SessionSource(platform="delegate", chat_id="{key}_delegate", chat_type="dm")`
- CLI: `SessionSource(platform="cli", chat_id="{session_name}", chat_type="dm")`

## Implementation Status

1. **core/session.py**: SessionSource, build_session_key(), ResetPolicy, SessionEntry, SessionDB (source-only API)
2. **platforms/base.py**: MessageEvent with chat_type, thread_id, chat_name, user_name + to_session_source()
3. **platforms/wecom.py, feishu.py**: Populate new fields, removed manual session_key construction
4. **gateway/server.py**: Uses to_session_source() + build_session_key()
5. **core/agent.py**: chat() takes SessionSource, derives session_key internally
6. **gateway/commands.py**: Uses get_entry() for session lookups
7. **tools/cronjob_tools.py**: Creates SessionSource for cron sessions
8. **tools/delegate_tool.py**: Creates SessionSource for delegate sessions
9. **cli/chat.py**: Creates SessionSource for CLI sessions
