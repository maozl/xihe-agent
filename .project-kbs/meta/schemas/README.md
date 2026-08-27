# 页面模板

完整的页面模板（Concept / Entity / Story / Change / Insight / Candidate）见
[../../PROTOCOL.md](../../PROTOCOL.md) 的「页面模板」章节。

本目录预留给项目特定的模板覆写或补充。当前为空。

## 快速参照

| 类型 | 用途 | PROTOCOL.md 章节 |
|------|------|------------------|
| Concept | 架构概念、技术规范、设计模式、编码约定 | Concept 页面模板 |
| Entity | 服务、中间件、数据库、API、表结构、部署、依赖 | Entity 页面模板 |
| Story | 需求、用户故事、功能改进、缺陷修复 | Story 页面模板 |
| Change | 代码变更记录、影响面评估、回滚方案 | Change 页面模板 |
| Insight | ADR、选型结论、踩坑总结、方案对比 | Insight 页面模板 |
| Candidate | 待验证方案草稿、不稳定临时笔记 | Candidate 页面模板 |

## 文件约定

- 文件名: `NNNN_slug.md`（四位序号 + 小写连字符 slug，序号按 `wiki/index.md` 顺序分配）
- 每页顶部放简短摘要
- 交叉链接用 `[[slug]]` 或相对路径
- 时间格式: `YYYY-MM-DD` 或 ISO 8601
