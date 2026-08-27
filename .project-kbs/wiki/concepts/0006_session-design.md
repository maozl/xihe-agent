---
type: concept
title: 会话两层 ID 设计
slug: 0006_session-design
aliases:
  - SessionDB
  - session_key
  - SessionSource
tags:
  - architecture
  - session
  - core
status: active
created: 2026-07-01
updated: 2026-07-30
related_pages:
  - wiki/entities/0001_xihe-agent.md
  - wiki/changes/0020_session-management-commands.md
sources:
  - path: raw/sources/session-design.md
    date: 2026-07-01
---

# 会话两层 ID 设计

## 摘要

会话用**两层 ID**: `session_key`（逻辑标识，「哪段对话」，确定性）+ `session_id`（物理实例，「哪一轮」，重置后变化）。所有消息存 `session_id`，`session_key` 只做路由、映射到当前活跃 `session_id`。对外只暴露 source-only API（`get_or_create_session(SessionSource)`），key 的生成内部封装。会话 key 同时是 cron job / 中断的单元。

## 核心要点

- **`session_key`（逻辑）**: 由消息来源确定性计算，同一对话永不改变。
  格式: `agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}][:{user_id}]`，例 `agent:main:wecom:dm:chat123`。
- **`session_id`（物理）**: `{YYYYMMDD_HHMMSS}_{uuid8}`，重置时换新。
- **`chat_type`**: `dm`（Direct Message 私聊，按 chat_id 隔离）/ `group`（按 chat_id+user_id，若 `group_sessions_per_user`）/ `channel`（按 chat_id）/ `thread`（按 thread_id，默认跨用户共享）。
- **`thread_id`**: 子话题 ID（Telegram topic / Discord thread / Slack thread_ts）。**DM thread seeding**: 新建 DM thread 会话时自动把父 DM 历史拷进来，上下文延续。
- **`build_session_key(source)` 规则**:
  - DM: 有 chat_id+thread_id 用两者；只有其一用其一；都没有 → `...:dm`。
  - 群/频道: base `...:{chat_type}:{chat_id}`，可选追加 `:{thread_id}`；群隔离追加 `:{user_id}`。
- **`SessionSource`**: 消息来源数据类（platform / chat_id / chat_name / chat_type / user_id / user_name / thread_id）。平台适配器的 `MessageEvent.to_session_source()` 构造它；`XiheAgent.chat(source, msg, ...)` 只收 source。
- **特殊来源**: cron → `SessionSource(platform="cron", chat_id="cron_{job_id}_{ts}")`；delegate → `platform="delegate"`；CLI → `platform="cli"`。
- **重置策略** (`config.yaml` 的 `session`): `idle`（N 分钟不活动）/ `daily`（每日定点）/ `both`（先到先重置）/ `none`。支持 per-platform 覆盖。重置 = 新 session_id + 空历史（可选 seed 前序最后几轮）+ 通知用户。**有活跃后台进程（terminal/browser）的 session 不重置**。
- **system prompt 缓存**: 存 SQLite 的 `system_prompt` 字段，下次读回原样保证 prompt cache 前缀匹配（持续会话不重建）。

## 适用场景

- 新增平台适配器: 实现 `MessageEvent` + `to_session_source()`，不要手动拼 session_key。
- 调整会话隔离粒度: 改 `group_sessions_per_user` / `thread_sessions_per_user`。
- 排查「会话串了」: 检查 chat_type / thread_id / user_id 是否参与了 key 生成。

## 涉及文件

`core/session.py`（SessionSource / build_session_key / ResetPolicy / SessionEntry / SessionDB）/ `platforms/base.py`（MessageEvent）/ `platforms/wecom.py`/`feishu.py` / `gateway/bot.py` / `gateway/commands.py` / `tools/cronjob_tools.py` / `tools/delegate_tool.py` / `cli/chat.py`。

## 相关页面

- [[0001_xihe-agent]] — 会话是 agent loop 的持久化基础
- 原文快照: [raw/sources/session-design.md](../../raw/sources/session-design.md)
