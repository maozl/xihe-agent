# 项目 Wiki

本项目使用 `.project-kbs/` 维护项目知识（架构决策、设计规范、踩坑经验），跨会话、跨阶段复用。

## 快速导航

- **协议与模板**: [PROTOCOL.md](PROTOCOL.md) — 完整工作流、知识模型、页面模板（权威）
- **当前活跃工作**: [wiki/active.md](wiki/active.md)
- **最近更新**: [wiki/recent.md](wiki/recent.md)
- **页面索引**: [wiki/index.md](wiki/index.md)
- **维护日志**: [wiki/log.md](wiki/log.md)
- **健康度**: [meta/lint-status.json](meta/lint-status.json)
- **候选笔记**: [meta/candidates/index.md](meta/candidates/index.md)

## 目录结构

```
.project-kbs/
├── PROTOCOL.md          协议与模板（权威）
├── PROJECT_WIKI.md      本文件（导航）
├── raw/sources/         原始材料快照（仅追加）
├── wiki/
│   ├── active.md        当前活跃工作
│   ├── recent.md        最近更新
│   ├── index.md         页面索引
│   ├── log.md           维护日志
│   ├── concepts/        概念/规范
│   ├── entities/        实体（服务/中间件/依赖）
│   ├── stories/         需求/用户故事
│   ├── changes/         代码变更记录
│   └── insights/        决策/踩坑/ADR
└── meta/
    ├── lint-status.json 健康度元数据
    ├── candidates/      候选笔记（待验证）
    └── schemas/         页面模板（指向 PROTOCOL.md）
```

## 会话开始检查清单

1. 读 `meta/lint-status.json`，若 `last_lint` 超过 24 小时则提醒整理。
2. 读 `wiki/active.md` 了解当前工作。
3. 项目讨论 / 选型 / 评审前，按 PROTOCOL.md「检索顺序」查询知识库。
