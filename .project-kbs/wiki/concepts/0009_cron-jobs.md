---
type: concept
title: 定时任务（cron）设计
slug: 0009_cron-jobs
aliases:
  - cron
  - 定时任务
  - scheduled jobs
tags:
  - tools
  - cron
  - scheduler
status: active
created: 2026-07-03
updated: 2026-07-03
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/changes/0010_cdp-default-and-cron-job-forms.md
sources: []
---

# 定时任务（cron）设计

## 摘要

`tools/cronjob_tools.py` 提供定时任务能力。调度外壳：gateway 进程里的 daemon 线程，每 60s tick，每个 job 到点在一个**全新无状态 agent 会话**里执行（禁 `cronjob` toolset 防递归）。在此基线上叠加 **job 多形态 + 显式跨次状态**模型：job 不再只是 prompt，还可以是脚本驱动，从而支持「循环任务的增量推进 + 去重 + 零 token 静默」。

## 调度基线

- daemon 线程在 gateway 进程内，60s tick（`_scheduler_loop`）。
- 每次 `_execute_job` 新建会话（`SessionSource(platform="cron", ...)`），**无记忆**：不能问澄清问题、不被上次会话污染。
- 因此 **prompt 必须自包含**；跨次进度要 job 自己用文件持久化（读 → 处理 → 写回）。
- job 状态存 `~/.xihe-agent/cron/jobs.json`（xihe 自动管理，勿手编）；输出存 `~/.xihe-agent/cron/output/<job_id>/`。

## job 的三种形态（`_execute_job` 执行流）

按任务逻辑选择，不默认偏向：

| 形态 | 字段 | 何时用 |
|---|---|---|
| **纯 prompt** | `prompt` | 需要对新鲜/变化内容做推理（每日 briefing、摘要、决策建议） |
| **纯脚本** | `script` + `no_agent=true` | 确定性/机械任务（清理、数据同步、看门狗）；**0 token**，脚本 stdout 即投递结果 |
| **脚本喂 prompt** | `script`（不带 no_agent） | 脚本先跑产出数据，注入 `## Script Output` 后再让 agent 处理 |

## wake gate（静默跳过 agent）

脚本最后一行输出恰好 `{"wakeAgent": false}` → 本轮跳过 agent、静默、0 token。用于「只有新东西才打扰我」的循环任务：脚本自己做 diff（对比自管状态文件），没新内容就 `wakeAgent:false`，有才唤醒 agent。解析在 `_run_job_script`。

## context_from（链式管道）

`context_from=<other_job_id>` → 把那个 job 最近一次 output（`output/<id>/*.md`）截断到 8K 注入本次 prompt 前。用于 A→B→C 串联。

## 脚本约定

- 可复用脚本放 `~/.xihe-agent/scripts/`（用户级）或项目 `scripts/`（版本管理）；`script` 字段填纯文件名，`_resolve_script` 按绝对路径 → CWD → `~/.xihe-agent/scripts/` → 项目 `scripts/` 解析。
- 扩展名决定执行方式：`.py` → 当前解释器；`.ps1` → powershell；其余 shell（`_run_job_script` 按扩展名派发，避免 Windows 下 `shell=True` 跑 `.py` 失败）。
- **一次性产物**（数据缓存、调试 dump）放 `scratch/<任务名>/`，不堆项目根目录（见 `BEHAVIOR_RULES` 第 6 条）。

## 提示词引导

`core/prompts.py` 的 `CRON_GUIDANCE`（`cronjob` 工具加载时注入）指导 agent 按任务逻辑选形态、自建脚本、用 wake gate、链式编排。要点：判定该用脚本时 agent **自己 write_file 到 `~/.xihe-agent/scripts/`** 并自测，不让用户手写。

## 不在当前实现内（后续可补）

per-job 覆盖 model/provider/toolset/workdir/profile、at-most-once（执行前 advance_next_run）、跨进程文件锁、prompt-injection 扫描。xihe 暂未做。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — cronjob 工具的注册与 check_fn 门控
- [[0010_cdp-default-and-cron-job-forms]] — 本次落地的代码变更记录
