---
type: candidate
title: 候选: xihe-desktop ClaudeRunner 长驻 stream-json 重写（已验证，promoted）
slug: desktop-claude-longlived-rewrite
status: promoted
created: 2026-08-12
updated: 2026-08-12
resolved_at: 2026-08-12
resolution_target: wiki/changes/0027_desktop-claude-longlived-rewrite.md
resolution_note: 冷 resume 实测通过（kill 进程 A → 进程 B --resume 答 BLUEFIRE），双路径验齐，提升为正式 Change。
related_topic: 0026_desktop-agent-model-built-in-xihe
derived_from:
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
why_it_matters: F2 的性能/复用升级（每轮新进程 → 一会话一长驻进程），代码已落地 + tsc/build 绿，但唯一未验的「冷 resume」假设被 claude 自身损坏挡住；恢复后需先验冷 resume 再视作完成。
next_action: claude 修好后重跑冷 resume 探测（进程 A turn1 捕 sid → kill → 进程 B --resume 发 turn2，看 B 是否记得 turn1）；通过则提升为正式 Change 页 + 删本候选。
---

# 候选: xihe-desktop ClaudeRunner 长驻 stream-json 重写（已验证，promoted）

**已提升为正式 Change：[[0027_desktop-claude-longlived-rewrite]]。**

## 决议

- 结果: promoted
- 目标: [[0027_desktop-claude-longlived-rewrite]]
- 理由: 代码完成 + tsc/build 绿 + 热路径(同进程多轮 cache 复用) + 冷路径(kill 进程 A → 进程 B `--resume` 答 BLUEFIRE)双路径实测通过,最后一个未验假设成立。

详见目标页。本候选保留作历史线索。
