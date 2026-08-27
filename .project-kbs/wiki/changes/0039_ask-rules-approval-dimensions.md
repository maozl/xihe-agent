---
type: change
title: ask 规则、审批记忆落盘与维度、cron 审批卡闭环
slug: 0039_ask-rules-approval-dimensions
change_type: feature
risk_level: medium
status: completed
created: 2026-08-25
updated: 2026-08-25
affected_modules:
  - tools/_approvals.py
  - tools/cronjob_tools.py
  - core/agent.py
  - gateway/serve.py
  - gateway/bot.py
  - tools/clarify_tool.py
related_insights:
  - wiki/concepts/0037_approval-permission-system.md
---

# ask 规则、审批记忆落盘与维度、cron 审批卡闭环

## 摘要

审批系统从「危险操作才问」扩展为「**config 可圈定任意工具需确认**（ask 规则）+ 记忆落盘跨重启/跨维度共享（会话/定时任务名/工作空间）+ 定时任务弹出审批卡到聊天、批复后以后静默放行」。四步需求递进：write 工具纳入拦截（要求通用性 → 通用 ask 规则）→ 记忆持久化 → `memory_days` 语义收敛（默认 30，非法值回落，无 0 开关）→ cron 按任务名读记忆 + 工作空间按目录 + cron 审批卡闭环（用户否决「手动触发改同步执行」，定调 cron 本来就是后端执行、改为把审批卡投到 deliver 聊天）。

## 变更内容

### 1. ask 规则（通用工具圈定）

- `evaluate()` 在 allow 规则/记忆之后、危险判定之前插 `ask` 规则层（`_approvals.py`）：`approvals.ask: ["write_file", "patch"]` 加行即拦，无需改码。**置于 allow 之后** → allow 可 carve-out（`ask: [write_file]` + `allow: ["write_file(src/**)"]` = src/ 免问）。
- 摘要 `_generic_summary`：从常见参数名（path/command/url/name/action/query）拼一句——规则不挑工具，摘要也不能挑。
- `rule_text`：`write_file`/`patch` 取 `path` 并**反斜杠→正斜杠**（Windows 路径才匹配得上 fnmatch 限定符）。
- always 批准记 `"ask:{rule}"` 键——同一规则本维度免问，另一条 ask 规则各自记。
- `_HIGH_RISK` 表注释明确分工：固有危险动作进表；日常工具（write_file/patch）按需圈定走 ask 规则，**别往表里加恒危险工具**。

### 2. 审批记忆落盘（`agent_home/approvals/`）

- 按桶分文件（多进程安全，避免单文件互踩）：`<sanitized键>.<sha1[:8]>.json`，条目 `{tool, key, ts}`。
- 读侧 `_rules_for` 懒水合（首访读盘一次，`_hydrated` 去重）；`_load_session` TTL 过滤、坏文件 fail-open 按空（宁可重问不可误放）。
- 写侧 `_save_session` tmp + `os.replace` 原子全量写；全量条目统一刷 ts → 活跃桶**滑动续期**、闲置自然过期。
- `_maybe_sweep` 每进程一次按 mtime 清过期文件（防已删会话孤儿堆积）。
- `remember_rule` 先水合再改再存——重启后第一次 always 批准不能把盘上旧记忆冲掉。
- `approvals.memory_days`：**默认 30，非法值（含 0/负数/非数字）回落 30**（用户定调：不考虑 0 开关）。TTL 只在读侧/清扫侧生效，写侧只刷 ts（`_save_session` 因此不带 cfg 参数）。
- 删除死代码 `needs_approval()`（0 调用方）；测试改走 `evaluate`/`_danger_detail`。

### 3. 记忆维度（approval_key 机制）

- `XiheAgent.chat(..., approval_key=...)`：传入则审批桶用它，默认仍 session_key。**只换审批桶，不动历史/会话键**；子代理守卫不变（顶层写、子代理共享引用读）。
- **cron**：`_execute_job` 传 `cron_job:{任务名}`（按名称非 job_id——用户口径；同任务所有运行、跨进程共享）。
- **工作空间**：serve `_handle_send` 有 `cwd`（桌面绑定空间就会发）时传 `ws:{规范化目录}`（`_ws_approval_key`：正斜杠 + lower + 去尾斜杠，Windows 盘符大小写不敏感）；未绑定维持按对话。桌面端零改动。

### 4. cron 审批卡闭环（gateway）

- `cronjob_tools._make_approval_callbacks(job, agent)`：有 deliver 通道（`_resolve_delivery_target` + adapter）→ request 回调把审批卡（任务名 + y/n/a 指引）发到目标聊天并**登记后台路由表**；result 回调注销 + 回投「✅ 已批准 / ❌ 已拒绝（原因）」。无通道（local/无 adapter/serve）→ (None, None)，维持无人值守即拒。
- **后台路由表**（`_approvals.py`）：`_pending_external[(platform, chat_id)] → [(id, resolve)]`；`resolve_pending_reply` 整词 y/n/a 折给**最新一张卡**。**按实际投递的适配器名登记**（全路径检查发现的坑：按 deliver 里写的平台名登记，`platform:chat_id` 形式与回复通道对不上 → 永远等不到折批复）。
- gateway `_handle_steer` 在活动 agent 折批复之后、开新 turn 之前查路由表——活动 turn 的审批优先于 cron 挂卡。
- 卡片发送失败 → request_cb 抛错 → request_approval **立即拒绝**（不空等超时）。
- 任务删除/暂停 → `_interrupt_job_run` 直接 `agent.interrupt()`，挂在审批等待上也立即解除（`_active_agents` 表）。
- `_deliver_result` 抽出 `_send_to_chat`（审批卡/结果回投复用）。

### 5. 顺带

- `clarify_tool.py`（外部 /btw 评审，1+2+3 采纳）：删无意义的 `check_fn`（恒真；108/113 工具带 check_fn，无 check_fn=无条件可用是合法形态）；options 去重/strip/截 10 条；结果加 `instruction`（停下等待用户回答，别继续跑其他工具）。
- config.example.yaml approvals 段全面同步（ask/维度/落盘/cron 卡）；桌面 SettingsPanel 审批模式 hint 修正（原「终端审批」措辞误导）。

## 关键设计取舍

| 决策 | 备选 | 理由 |
|---|---|---|
| 通用 ask 规则（config 一行拦任意工具） | `approvals.write` 布尔 + `_WRITE_TOOLS` 硬编码 | 用户明确要求通用性（后续加其他工具不改码） |
| cron 记忆按任务名 + 审批卡投递 | 手动触发改内联同步执行（借聊天回调喂记忆） | 用户否决：cron 本来就是后端执行；审批卡闭环同效且不阻塞聊天轮 |
| 按桶分文件 | 单 JSON 文件 | gateway/serve/CLI 多进程共用 agent_home，单文件全量写互踩 |
| 路由表按适配器名登记 | 按 deliver 目标平台名 | 回复永远从实际投递的适配器回来 |
| 桌面 serve 的 cron 审批卡二期 | 本期一起做 | serve 无 platform adapter（cron 结果本就投不出去），需 WS adapter + 桌面 UI 卡，大头在桌面端 |

## 已知边界

- **飞书**：适配器不消费 steer handler（历史缺口，非本次引入）→ 飞书上 cron 卡的回复折不进去、等超时拒绝；企微（本部署）全链路可用。
- 同聊天多张挂卡：回复按最新一张解析；旧卡等超时。
- 挂卡期间该聊天发整词 "y/n/a" 会被当批复吃掉（与活动 turn 行为一致）。
- `ws:` 键整路径 lower——Windows 正确；Linux 会合并大小写不同的目录（部署仅 Windows）。

## 验证

- `tests/test_approvals.py`：+ask 规则 6 测（整工具/默认不拦/路径限定符 Windows 分隔符/allow carve-out/deny 压 ask/同规则会话记忆）+ 落盘 5 测（重启持久/非法 memory_days 回落默认/过期淘汰/坏文件 fail-open/重启后首批不冲旧记忆）+ 路由表 4 测 + approval_key 换桶 + ws 键规范化。
- `tests/test_cronjob_approval.py`（新文件，7 测）：任务名桶传递/无通道不接回调/有通道接线/审批卡往返（发卡→折 a→always→决议回投→路由表清）/发卡失败立即拒/任务打断解除审批等待。
- pytest 全量绿（exit=0）；conftest 增 `_pending_external` 隔离。
- 真机 agent_home 落盘往返 smoke 通过后清理。

## 相关页面

- [[0037_approval-permission-system]] — 系统概念页（本次同步订正：ask 层、落盘、维度、后台审批节）
- [[0038_dangerous-operation-approvals]] — 系统首版落地变更
- [[0009_cron-jobs]] — cron 三形态与调度（agent 路径即本次接审批的路径）
