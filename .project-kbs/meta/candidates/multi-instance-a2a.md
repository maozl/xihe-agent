# 候选：多实例 + A2A 协同架构

> **状态**: open（待决定）
> **日期**: 2026-07-27
> **评估结论**: 当前场景（单用户桌面工具）大概率过度工程化，投入产出比不高。建议先用 skill 系统 + delegate_task 覆盖，遇到瓶颈再回来。

## 目标

xihe 做成可启动多个实例，每个实例地位对等，通过 system_prompt 区分功能，相互通过 A2A 协议对话，完成多 agent 协同。

## 方案（分 4 阶段，~10h）

| 阶段 | 内容 | 工作量 |
|---|---|---|
| Phase 1 | per-instance system_prompt 配置入口（config.yaml/env） | ~1h |
| Phase 2 | HTTP /a2a server（aiohttp，复用 XiheAgent.chat） | ~4h |
| Phase 3 | peer 注册表 + a2a_call 工具 + delegate_task 扩展 | ~3h |
| Phase 4 | 编排（system_prompt 引导路由 + 验证） | ~2h |

架构图：
```
┌─────────────┐       WeCom        ┌──────────────┐
│  User       │ ←─────────────────→ │  xihe (A)    │
│  (用户)      │                     │  协调者       │
└─────────────┘                     └──────┬───────┘
                                           │ HTTP /a2a
                              ┌────────────┼────────────┐
                              ↓            ↓            ↓
                        ┌──────┐    ┌──────┐    ┌──────┐
                        │xihe B│    │xihe C│    │xihe D│
                        │ITSM  │    │Data  │    │Monitr│
                        └──────┘    └──────┘    └──────┘
```

## 已具备的基础

- ✅ 进程隔离（每个 xihe 独立 Python 进程）
- ✅ AGENT_HOME + BROWSER_CDP_PORT 可 per-instance 配
- ✅ 12-factor config（env > YAML）
- ✅ system_prompt_override 有钩子（缺配置入口）
- ✅ delegate_task 支持 in-process 子 agent
- ✅ Skills per-AGENT_HOME 隔离

## 缺口

1. ❌ per-instance system_prompt 从 config 读（Phase 1）
2. ❌ HTTP server（A2A 入口）— gateway 目前只有 WebSocket client
3. ❌ peer 发现 + 远程委托工具
4. ❌ 编排/路由

## 评估：是否真实需求？

**结论：当前场景大概率不是刚需。**

### 反对理由
1. **单用户桌面工具**：一个人用，跑 4+ 进程运维重、收益微。
2. **delegate_task 已覆盖 90%**：子 agent 可以设不同 system_prompt / toolset / model，在一个进程内实现"multi-agent collaboration"。
3. **skill 系统已覆盖特化**：加载不同 skill = 不同领域专家。不需要单独进程。
4. **A2A 开销**：HTTP 往返 + 序列化 + 超时/故障面 vs in-process 函数调用（0 延迟）。
5. **LLM 成本 4x**：每实例独立对话。

### 什么时候才真正有价值
1. **多人团队**：每人一个 xihe（多用户，不是多 agent）。
2. **真并行长任务**：一个 agent 跑 30 分钟扫描，同时另一个响应实时消息。
3. **不同 LLM 提供商/API client**：需要完全不同的配置。
4. **跨机器部署**：ITSM agent 在内网 A，DataMap agent 在内网 B，网络隔离。

### 建议
- **先不做。** 把精力投在 skill 质量、跨系统 skill 编排、定时自动化上。
- **delegate_task 先顶着**：如果需要"不同角色 agent"，用子 agent + 不同 system_prompt。
- **遇到具体瓶颈再回来评估**：能说出"单进程搞不定的具体场景"时再做多实例。

## 相关
- [[0002_tool-registry-and-dispatch]] — 工具注册/调度
- [[0017_role-based-subagents]] — delegate_task 子 agent 角色
- [[0018_tool-skill-workflow-role-layering]] — 工具/skill/workflow/role 分层
