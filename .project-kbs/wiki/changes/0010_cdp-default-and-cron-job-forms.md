---
type: change
title: 浏览器改 CDP 托管默认 + cron job 多形态 + 提示词约定
slug: 0010_cdp-default-and-cron-job-forms
change_type: feature
risk_level: medium
status: completed
created: 2026-07-03
updated: 2026-07-03
affected_modules:
  - tools/browser_tool.py
  - tools/cronjob_tools.py
  - core/toolsets.py
  - core/prompts.py
related_concepts:
  - wiki/concepts/0003_browser-tools.md
  - wiki/concepts/0009_cron-jobs.md
rollback_plan: 内联（见下）
---

# 浏览器改 CDP 托管默认 + cron job 多形态 + 提示词约定

## 摘要

三组已验证落地的改动：(1) 浏览器默认改用 CDP 托管真实 Chrome，解决内网 SSO（门户/passport）登录态保不住的问题；(2) cron 支持 job 多形态 + wake gate + context_from，支持循环任务的增量去重与零 token 静默；(3) 提示词新增 scratch 目录约定、CRON_GUIDANCE、撞登录墙主动开登录页的行为。

## 变更一：浏览器 CDP 托管默认（`tools/browser_tool.py`、`core/toolsets.py`）

### 背景（踩坑）
原默认是 `launch_persistent_context` 的 Playwright Chrome，对 门户/passport SSO **登不进**：SSO 回调走 `http://<内部门户域名>/?tmploginticket=...`，而 `SSO token` 是 `Secure` cookie；全新 Playwright context 无 HSTS 记忆 → 走 http → RFC 6265 丢弃 Secure cookie → 跳回登录页。真实 Chrome 有 HSTS 缓存会把 http 抬成 https，Secure cookie 才存住。

### 改动
- 新增 `_CDP_PROFILE_DIR`（`~/.xihe-agent/browser/cdp-profile`）/`_CDP_PORT=9222`/`_CDP_URL`（用字面量 `127.0.0.1`，规避 IPv6 `::1` 拒连）。
- 新增 `_find_system_chrome` / `_cdp_port_open` / `_launch_cdp_chrome`（分离进程，不随 xihe 退出）/ `_ensure_cdp_browser`（连 CDP，含 greenlet 跨线程 `_full_restart` 保护）。
- `_ensure_browser` 改为**默认 CDP**，CDP 起不来才降级 persistent（带 warning）。
- 新增工具 `browser_logout`：按域名清 cookies+localStorage（含 SSO 父域），`wipe_profile=true` 删 cdp/profile 做彻底重置；补进 `core/toolsets.py` 的 `web` toolset（顺带把原本"注册未列"的 `browser_login` 也补进 toolset——之前它对 agent 不可见）。
- `browser_navigate` 的 `sso_hint` 简化；`browser_connect`/`browser_login` 描述对齐新逻辑。

### 验证
实测（`agent.log` 2026-07-03 09:49/09:54/10:56 `Connected to CDP-managed browser`）：`browser_navigate(<ITSM域名>)` 直接读到工单（cdp-profile 有 ITSM 登录态）；<文档系统域名> 因无登录态显示「请登录」（CDP 不能替用户过 SSO，需在该 CDP 窗口登一次）。一个窗口、登录态跨重启保留。

## 变更二：cron job 多形态（`tools/cronjob_tools.py`）

### 背景
xihe 原 cron 把「无状态纯 prompt」当唯一模型；worldcup 式循环任务每次全量重拉/重算/重交、烧满 token。本次在同一无状态基线上叠加 job 多形态 + 显式跨次状态。

### 改动（详见 [[0009_cron-jobs]]）
- 新增 `_SCRIPTS_DIR`（`~/.xihe-agent/scripts/`）、`_resolve_script`、`_run_job_script`（按扩展名派发 + wake gate 解析）、`_save_script_output`。
- `_execute_job` 重写为三形态：纯 prompt / `no_agent` 纯脚本（0 token）/ 脚本喂 prompt（wake gate 可跳过 agent）；`context_from` 注入指定 job 上次 output。
- `_create_job`/`_list_jobs`/工具 schema 加 `script`/`no_agent`/`context_from`。向后兼容老 job。

### 验证
`_run_job_script` 单测：wake gate（`{"wakeAgent": false}` 正确剥离 + wake=False）、no-wake（wake=True）、`~/.xihe-agent/scripts/` 解析、缺失返回 None 均通过。

## 变更三：提示词（`core/prompts.py`）

- `BEHAVIOR_RULES` 加第 6 条：一次性产物写 `scratch/<任务名>/`，可复用脚本写 `scripts/`，不堆项目根目录。
- 新增 `CRON_GUIDANCE`（`cronjob` 工具加载时注入）：按任务逻辑选形态、自建脚本、wake gate、context_from。
- `BROWSER_LOGIN_GUIDANCE`：撞登录墙（请登录/无权限/`sso_hint`/空 snapshot）时**主动 navigate 到登录页**让用户登，而非只问；明确 CDP Chrome 是独立 profile，只记在其内登过的站点。

## 影响面

- 浏览器：所有 `browser_*` 调用现在走 CDP Chrome；无系统 Chrome 时降级 persistent（行为等同旧版）。
- cron：老 job（纯 prompt）不受影响；新能力 opt-in。
- 提示词：需**重启 gateway** 生效（system prompt 按会话缓存，工具 schema/模块全局态也是进程级缓存）。

## 回滚

- 浏览器回 persistent：把 `_ensure_browser` 里的 `if _ensure_cdp_browser(): return True` 去掉，直接 `return _launch_browser()`。
- cron 回纯 prompt：`_execute_job` 跳过 script/context_from 分支即可（或删 job 的 `script` 字段）。
- 提示词：三段均在 `core/prompts.py`，直接还原字符串。

均为单文件可逆改动，无数据迁移。

## 相关页面

- [[0003_browser-tools]] — 浏览器工具概念（已同步订正为 CDP 默认）
- [[0009_cron-jobs]] — cron 设计概念
