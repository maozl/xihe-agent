---
type: concept
title: 浏览器工具集
slug: 0003_browser-tools
aliases:
  - browser tools
  - Playwright 工具
tags:
  - tools
  - browser
  - playwright
status: active
created: 2026-07-01
updated: 2026-07-03
related_pages:
  - wiki/entities/0001_xihe-agent.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/changes/0010_cdp-default-and-cron-job-forms.md
sources:
  - path: raw/sources/browser-tools.md
    date: 2026-07-01
---

# 浏览器工具集

## 摘要

基于 Playwright 的网页自动化工具集（`tools/browser_tool.py`）。设计三原则：**独立工具拆分**（每个操作一个工具，LLM 选择更精准）、**Accessibility Tree + ref ID**（结构化快照 + 精准交互）、**CDP 托管优先的认证架构**。模块级全局状态（`_page`/`_context`/`_browser_instance`）跨网关消息持久。主模型非多模态，`browser_vision` 的图像分析走 AuxiliaryClient。

> ⚠️ 启动策略已于 2026-07-03 改为 **CDP 托管真实 Chrome 为默认**（见 [[0010_cdp-default-and-cron-job-forms]]）。旧的「持久化 Playwright profile 为主」已降为兜底——因 Playwright 全新 context 无 HSTS 记忆，会在 http SSO 回调上丢弃 Secure cookie（如内部门户的 `SSO token`），登不进内网 SSO。

## 核心要点

- **工具族**（按职责）:
  - 导航: `browser_navigate` / `back` / `forward` / `reload` / `close`
  - 快照: `browser_snapshot`（a11y tree + ref ID `[@e1]`）/ `browser_screenshot` / `browser_vision`（走 aux vision）
  - 交互: `browser_click` / `type` / `hover` / `select` / `scroll` / `press` / `drag` / `check` / `uncheck` / `upload`
  - 同步: `browser_wait`（text/selector/url/load_state/function/超时 多模式）
  - 标签页 / iframe / cookies / console / eval
  - 认证: `browser_state_save|load|list|delete` / `browser_login` / `browser_logout`（清登录态）/ `browser_connect`（手动接管外部 CDP）
- **标准工作流**: `navigate → wait(加载) → snapshot(拿 ref) → click/type(@ref) → snapshot(变化后重取)`。ref ID 优于 CSS selector / 文本 fallback。
- **启动策略（默认 CDP 托管，递进回退）**:
  1. **CDP 托管真实 Chrome（默认）**: xihe 自己拉起一个独立 profile（`~/.xihe-agent/browser/cdp-profile`）+ `--remote-debugging-port=9222` 的真实 Chrome 并 CDP 接管。真实 Chrome 带 HSTS 记忆 → Secure SSO cookie 存得住；分离进程，登录态跨 xihe 重启保留。**CDP 不能替用户过 SSO/扫码/OTP**——只记在它窗口里登过的站点。
  2. **persistent Playwright（降级兜底）**: 无系统 Chrome 或 9222 连不上时，`_ensure_browser` 回退到 `launch_persistent_context(_PROFILE_DIR)`。对内网 SSO 多半走不通（见上 ⚠️）。
  3. **StorageState 导入导出**: `browser_state_save/load`（JSON，`~/.xihe-agent/browser/states/`），显式快照，辅助手段。
- **`browser_logout`**: 按域名清 cookies+localStorage（含 SSO 父域）；`wipe_profile=true` 删 cdp/profile 做彻底重置。
- **`browser_connect`**: 手动接管用户自己起的外部 CDP 浏览器（高级用法，URL 用 `127.0.0.1` 非 localhost）。
- **降级**: 无 Playwright 时 `browser_navigate` 降级为 httpx 抓取（只读无交互）——即「agent 没有浏览器工具」症状的根因（`check_fn` 门控）。
- **环境约束**: 本部署内网无公网，用系统 Chrome/Edge，**不**依赖 `playwright install chromium`。

## 适用场景

- 实现 / 维护浏览器工具时，遵循 ref ID 优先、CDP 默认 + persistent 兜底的启动策略。
- 排查「登不进 SSO」：先确认走的是不是 CDP（`agent.log` 找 `Connected to CDP-managed browser`）；persistent 对 SSO 走不通是已知坑。
- 排查「某站点无权限」：CDP Chrome 是独立 profile，只记在其内登过的站点；让 agent 主动开登录页，用户登一次即持久。
- 清登录态：`browser_logout(domain=...)` 或 `wipe_profile=true`。

## Playwright sync-API 约束（编辑时必看）

- **不要**在事件回调（如 `framenavigated`）里调 `page.*`——会死锁 greenlet 调度器。捕获工作放进 `add_init_script` 的 JS。
- `sessionStorage` / storage 访问要 guard：在 `about:blank` / 沙箱帧上抛 `SecurityError`，可能破坏弹窗导航。

## 相关页面

- [[0001_xihe-agent]] — 浏览器工具是核心工具族之一，模块级全局状态跨网关消息持久
- [[0002_tool-registry-and-dispatch]] — 浏览器工具的 `check_fn` 门控（Playwright 不可导入时整组消失）
- 原文快照: [raw/sources/browser-tools.md](../../raw/sources/browser-tools.md)
