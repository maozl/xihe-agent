---
type: concept
title: 上下文压缩机制
slug: 0004_context-compression
aliases:
  - ContextCompressor
  - 压缩
tags:
  - architecture
  - context
  - core
status: active
created: 2026-07-01
updated: 2026-07-01
related_pages:
  - wiki/entities/0001_xihe-agent.md
sources:
  - path: raw/sources/context-compression.md
    date: 2026-07-01
---

# 上下文压缩机制

## 摘要

当对话 token 接近模型上下文窗口阈值时，`ContextCompressor`（`core/compressor.py`）自动压缩历史，释放空间。核心是 4 步算法：裁剪旧 tool 结果 → 三段划分 → LLM 摘要中段 → 拼装并修复孤立 tool 对。摘要走 AuxiliaryClient（可用更便宜模型）。`core/agent.py` 的 `_chat_step` 每次调用前用 `should_compress` 判断。

## 核心要点

- **触发**: 估算 token ≥ `context_length × compression_threshold`（默认 0.50，见 `config.yaml`）。例: 128k × 0.5 = 64k。
- **Step 1 裁剪旧 tool 结果**: 远处（>200 字符）tool 输出替换为占位符，**保护最近 20 条**。tool 输出通常最长，先裁以减少后续摘要成本。
- **Step 2 三段划分**:
  - head（前 3 条: system prompt + 首轮）— 始终保留
  - tail（最近 ~20k tokens）— 当前上下文，始终保留
  - middle — 被摘要替换
- **Step 3 LLM 摘要 middle**: 首次用结构化模板（Goal / Progress / Key Decisions / Relevant Files / Next Steps）；增量更新时把上次摘要 + 新 turns 喂回，要求保留具体 file paths / commands / errors。目标 ~2000 tokens。
- **Step 4 拼装 + 修复**: `[head] + [摘要消息] + [tail]`。摘要消息 role 按前后 role 选择避免连续同 role；`_sanitize_tool_pairs` 补无 result 的 tool_call stub、删无 call 的 tool result。
- **token 估算**: `Σ(len(content)/4 + 10) + Σ tool_call.arguments/4`（4 字符≈1 token，每条 +10 开销），粗估不做精确 tiktoken。
- **压缩后重建 system prompt**: 见 CLAUDE.md——压缩后系统提示会重建（memory snapshot 更新），不是原样保留。

## 适用场景

- 调整压缩阈值 / 摘要模型时，参考 4 步算法定位改动点（`should_compress` / `compress` / `_prune_old_tool_results` / `_find_tail_start` / `_generate_summary` / `_sanitize_tool_pairs`）。
- 排查「长对话丢上下文」：看摘要是否触发、tail 保护是否够、摘要是否含关键 file path。

## 涉及文件

`core/compressor.py`（算法）/ `core/agent.py`（`_chat_step` 调用入口）/ `core/auxiliary_client.py`（摘要 LLM）。

## 相关页面

- [[0001_xihe-agent]] — 压缩是 agent loop 的不变量之一
- 原文快照: [raw/sources/context-compression.md](../../raw/sources/context-compression.md)
