# 项目 Wiki 管理协议

本文件是平台无关的。

可安装在 agent 系统支持的任何持久化指令面:系统提示词、项目指令、自定义指令、配置记忆、或每次会话开始时让 agent 读取的启动文件。

## 目的

与用户共同维护一个面向软件项目的、有据可查的项目知识层。

目标是把项目推进过程中形成的架构决策、技术选型、踩坑经验、模块信息等持久保存下来,跨会话、跨阶段复用。
这不是聊天记录归档、不是任务看板、也不是临时调试日志。

## 知识库根目录

`.project-kbs`

## 知识模型

- `raw/sources/`:需求文档、设计稿、会议纪要等原始材料的不可变快照
- `wiki/active.md`:当前活跃需求、进行中的变更、待决问题
- `wiki/recent.md`:最近新增、更新、修正或已过时的知识
- `wiki/index.md`:按内容分类的页面与来源目录
- `wiki/log.md`:按时间顺序记录的收录、回写、查询、整理操作
- `wiki/concepts/`:架构概念、技术规范、设计模式、编码约定等稳定知识
- `wiki/entities/`:服务、中间件、第三方依赖、数据库、团队角色等命名实体
- `wiki/stories/`:需求、用户故事、功能改进、缺陷修复及其验收标准
- `wiki/changes/`:代码变更记录、变更分析、影响面评估、回滚方案
- `wiki/insights/`:架构决策记录(ADR)、技术选型结论、踩坑总结、方案对比
- `meta/candidates/`:待验证的方案草稿、尚不稳定的临时笔记,后续可提升、合并或丢弃
- `meta/lint-status.json`:知识库健康度与新鲜度元数据
- `meta/schemas/`:页面创建和更新时参照的本地模板

## 页面模板

### Concept 页面模板 (概念/规范)

用于架构概念、技术规范、设计模式、编码约定等稳定知识。

**适用场景**:
- 同一架构概念或规范在多个模块、服务或项目中反复出现
- 需要作为可复用的技术约定长期保留
- 页面关注的是概念本身,而非具体服务、项目或决策

**Frontmatter 结构**:

```yaml
---
type: concept
title: 微服务拆分原则
slug: 0001_microservice-split-principles
aliases:
  - 服务拆分原则
  - 拆分策略
tags:
  - architecture
  - microservice
status: active
created: 2026-05-11
updated: 2026-05-11
related_pages:
  - wiki/stories/0001_user-center-auth-refactor.md
  - wiki/insights/0001_service-split-decision.md
sources:
  - path: raw/sources/architecture-review-202605.md
    date: 2026-05-11
---
```

**字段说明**:
- `aliases`: 值得检索或链接的别名,不必列出所有同义词
- `related_pages`: 最相关的关联页面,不需要完整图谱
- `updated`: 概念含义、框架或关键链接变更时更新

**正文结构**:

```markdown
# 微服务拆分原则

## 摘要

1-2 段以可复用方式定义该概念。

## 核心要点

- 业务边界优先,按业务能力而非技术层拆分
- 单一职责,一个服务只做一件事
- 高内聚低耦合,服务间通过明确定义的接口交互

## 适用场景

- 何时应用此原则
- 何时不适用

## 相关页面

- [[0001_user-center-auth-refactor]] - 当前应用此概念的需求
- [[0001_service-split-decision]] - 依赖此概念的决策记录
```

---

### Entity 页面模板 (实体)

用于服务、服务器、中间件、数据库、API接口、表结构、部署模板、第三方依赖等命名实体。

**实体类型**:
- `service`: 服务 (业务服务、后台服务)
- `server`: 服务器/集群 (物理机、K8s集群、云实例)
- `middleware`: 中间件 (Redis、Kafka、Nginx)
- `database`: 数据库 (MySQL、MongoDB)
- `api`: API接口 (核心业务接口)
- `table`: 表结构 (核心业务表)
- `deployment`: 部署模板 (Dockerfile、K8s配置)
- `dependency`: 第三方依赖 (SDK、开源库)

**适用场景**:
- 同一命名实体在多个来源或项目中出现
- 该实体需要独立的事实、角色或对比上下文
- 未来工作可能再次按名称引用它

**Frontmatter 结构**:

```yaml
---
type: entity
entity_type: service | server | middleware | database | api | table | deployment | dependency
title: 用户中心服务
slug: 0001_user-center-service
aliases:
  - user-center
  - ucs
tags:
  - service
  - backend
  - auth
status: active
created: 2026-05-11
updated: 2026-05-11
related_pages:
  - wiki/stories/0001_user-center-auth-refactor.md
  - wiki/insights/0001_auth-solution-decision.md
sources:
  - path: raw/sources/service-inventory.md
    date: 2026-05-11
---
```

**字段说明**:
- `entity_type`: 实体类型,决定正文结构的侧重点
- `aliases`: 别名、缩写或内部代号
- `related_pages`: 对复用或对比最重要的页面
- `status`: 通常为 `active` 或 `archived`

**正文结构示例 - 服务类型**:

```markdown
# 用户中心服务

## 摘要

用户中心服务,负责用户认证、权限管理、用户信息维护。

## 基本信息

- 服务地址: `user-center.internal:8080`
- 负责团队: 平台架构组
- 核心职责: 用户认证、权限管理、用户信息维护
- 代码仓库: `gitlab.internal/backend/user-center`

## 技术栈

- 框架: Spring Boot 2.7
- 数据库: MySQL 8.0 (主库) + Redis 7.0 (缓存)
- 消息队列: Kafka (用户事件)

## 依赖关系

- 上游: API网关、BFF层
- 下游: 权限服务、消息服务

## 关键配置

- 认证方式: JWT + Redis会话
- 限流策略: 令牌桶,1000 QPS/实例

## 相关页面

- [[0001_user-center-auth-refactor]] - 当前重构需求
- [[0001_auth-solution-decision]] - 认证方案决策
```

**正文结构示例 - 数据库类型**:

```markdown
# 订单主库

## 摘要

订单业务主库,承载订单、支付、退款等核心数据。

## 基本信息

- 类型: MySQL 8.0
- 连接串: `mysql-order-master.internal:3306/order_db`
- 环境: 生产环境
- 容量: 2TB (已用1.2TB)

## 分片策略

- 分片键: user_id
- 分片数: 16
- 分片规则: user_id % 16

## 核心表

- `order_main`: 订单主表
- `order_item`: 订单明细表
- `payment_record`: 支付记录表

## 备份策略

- 全量备份: 每日凌晨2点
- 增量备份: 每小时
- 保留周期: 30天

## 相关页面

- [[0001_order-service]] - 订单服务
- [[0001_order-table-design]] - 订单表结构设计
```

**正文结构示例 - API接口类型**:

```markdown
# 登录认证接口

## 摘要

用户登录认证接口,返回JWT Token。

## 基本信息

- 路径: `POST /api/v1/auth/login`
- 所属服务: 用户中心服务
- 认证方式: 无需认证(登录接口)
- 限流: 100 QPS/IP

## 请求参数

```json
{
  "username": "string",    // 用户名
  "password": "string",    // 密码
  "device_type": "string"  // 设备类型: web|mobile|client
}
```

## 响应示例

```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
    "expires_in": 900
  }
}
```

## 调用方

- Web前端
- 移动端App
- Linux客户端

## 变更历史

- 2026-05-15: Session改为JWT
- 2026-04-10: 新增device_type参数

## 相关页面

- [[0001_auth-solution-decision]] - 认证方案决策
- [[0001_auth-module-refactor]] - 认证模块变更
```

**正文结构示例 - 表结构类型**:

```markdown
# 用户表设计

## 摘要

用户中心核心表,存储用户基本信息。

## 表信息

- 表名: `user_main`
- 所属库: `user_center`
- 引擎: InnoDB
- 字符集: utf8mb4

## 字段定义

| 字段 | 类型 | 说明 | 索引 |
|-----|------|------|------|
| id | BIGINT | 主键 | PK |
| username | VARCHAR(64) | 用户名 | UK |
| password_hash | VARCHAR(128) | 密码哈希 | - |
| email | VARCHAR(128) | 邮箱 | UK |
| phone | VARCHAR(20) | 手机号 | UK |
| status | TINYINT | 状态: 0禁用 1正常 | IDX |
| created_at | DATETIME | 创建时间 | - |
| updated_at | DATETIME | 更新时间 | - |

## 索引设计

- PRIMARY KEY: `id`
- UNIQUE KEY: `uk_username` (username)
- UNIQUE KEY: `uk_email` (email)
- UNIQUE KEY: `uk_phone` (phone)
- INDEX: `idx_status` (status)

## 分片策略

- 分片键: `id`
- 分片数: 8

## 相关页面

- [[0001_user-center-service]] - 用户中心服务
- [[0001_user-center-auth-refactor]] - 用户中心重构
```

**正文结构示例 - 部署模板类型**:

```markdown
# 微服务标准部署模板

## 摘要

Spring Boot微服务的标准K8s部署模板。

## 基本信息

- 模板类型: Kubernetes Deployment
- 适用服务: 所有Spring Boot微服务
- 维护团队: 平台架构组

## Dockerfile模板

```dockerfile
FROM openjdk:11-jre-slim
WORKDIR /app
COPY target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
```

## K8s配置要点

- 副本数: 最小2个,支持HPA自动扩缩
- 资源限制: CPU 1核,内存 2Gi
- 健康检查: `/actuator/health`
- 配置管理: ConfigMap + Secret
- 日志采集: Filebeat sidecar

## CI/CD流程

1. 代码提交 → 触发Jenkins构建
2. 单元测试 → 构建Docker镜像
3. 推送镜像仓库 → 触发K8s部署
4. 灰度发布 → 全量发布

## 相关页面

- [[0001_user-center-service]] - 应用此模板的服务
- [[0001_canary-release-strategy]] - 灰度发布策略
```

---

### Story 页面模板 (需求/用户故事)

用于需求、用户故事、功能改进、缺陷修复及其验收标准。

**适用场景**:
- 记录需求背景、用户价值和验收条件
- 跟踪功能从需求到上线的完整上下文
- 管理需求变更和优先级调整

**Frontmatter 结构**:

```yaml
---
type: story
title: 用户中心认证重构
slug: 0001_user-center-auth-refactor
story_type: feature | improvement | fix | refactor
priority: high | medium | low
status: active
created: 2026-05-11
updated: 2026-05-11
assignee: 平台架构组
acceptance_criteria:
  - JWT认证方案上线并支持多端
  - 权限校验逻辑下沉到网关层
  - Session迁移完成,旧方案下线
related_changes:
  - wiki/changes/0001_auth-module-refactor.md
  - wiki/changes/0001_gateway-auth-integration.md
related_insights:
  - wiki/insights/0001_auth-solution-decision.md
  - wiki/insights/0001_cache-breakdown-protection.md
last_reviewed: 2026-05-11
---
```

**字段说明**:
- `story_type`: 故事类型,`feature`(新功能)、`improvement`(改进)、`fix`(修复)、`refactor`(重构)
- `priority`: 优先级
- `status`: 使用 `active`、`in_progress`、`completed` 或 `cancelled`
- `acceptance_criteria`: 验收标准列表
- `related_changes`: 关联的代码变更页面
- `related_insights`: 相关的决策记录

**正文结构**:

```markdown
# 用户中心认证重构

## 摘要

作为系统管理员,我希望统一多端认证方式,以便降低维护成本并提升系统扩展性。

## 背景

- 现有Session方案依赖Redis共享,高并发下存在性能瓶颈
- 权限校验逻辑耦合在业务代码中,难以维护
- 多端统一认证需求(Linux客户端、Web、移动端)

## 用户故事

**作为** 系统管理员
**我希望** 统一多端认证方式为JWT
**以便** 降低维护成本、支持水平扩展、简化多端集成

## 验收标准

- [ ] JWT认证方案上线并支持多端
- [ ] 权限校验逻辑下沉到网关层
- [ ] Session迁移完成,旧方案下线
- [ ] 性能测试通过,QPS提升50%以上
- [ ] 灰度发布验证无异常

## 范围

- 认证方案迁移 (Session → JWT)
- 权限校验逻辑下沉到网关
- 多端Token管理方案
- 灰度发布策略

## 排除范围

- 用户信息结构调整
- 权限模型重构

## 相关变更

- [[0001_auth-module-refactor]] - 认证模块代码重构
- [[0001_gateway-auth-integration]] - 网关认证集成

## 相关决策

- [[0001_auth-solution-decision]] - 认证方案选型结论
- [[0001_cache-breakdown-protection]] - 缓存击穿防护方案

## 相关服务

- [[0001_user-center-service]] - 目标服务
- [[0001_auth-gateway]] - 新增网关层

## 进展记录

- 2026-05-11: 需求确认,技术方案确定
- 2026-05-15: 开始JWT模块开发
```

---

### Change 页面模板 (代码变更)

用于代码变更记录、变更分析、影响面评估、回滚方案等。

**适用场景**:
- 记录具体代码变更的详细信息
- 进行影响面分析和风险评估
- 准备回滚和验证方案

**Frontmatter 结构**:

```yaml
---
type: change
title: 认证模块重构
slug: 0001_auth-module-refactor
change_type: refactor | feature | fix | optimization | migration
risk_level: high | medium | low
status: planned | in_progress | completed | rolled_back
created: 2026-05-11
updated: 2026-05-11
affected_services:
  - user-center-service
  - auth-gateway
affected_modules:
  - auth-module
  - permission-module
affected_apis:
  - POST /auth/login
  - POST /auth/logout
  - GET /auth/verify
related_story: wiki/stories/0001_user-center-auth-refactor.md
related_insights:
  - wiki/insights/0001_auth-solution-decision.md
rollback_plan: wiki/changes/0001_auth-module-rollback.md
---
```

**字段说明**:
- `change_type`: 变更类型
- `risk_level`: 风险等级
- `status`: 变更状态,`planned`(计划中)、`in_progress`(进行中)、`completed`(已完成)、`rolled_back`(已回滚)
- `affected_services`: 影响的服务列表
- `affected_modules`: 影响的模块列表
- `affected_apis`: 影响的接口列表
- `related_story`: 关联的需求/故事页面
- `rollback_plan`: 回滚方案页面

**正文结构**:

```markdown
# 认证模块重构

## 摘要

将用户中心认证模块从Session方案重构为JWT方案,涉及认证逻辑、Token管理、黑名单机制等。

## 变更内容

### 代码变更

- `AuthController.java`: 登录接口改为返回JWT Token
- `JwtTokenProvider.java`: 新增JWT生成和验证逻辑
- `TokenBlacklistService.java`: 新增Token黑名单服务
- `AuthInterceptor.java`: 移除Session校验,改为JWT校验
- `application.yml`: JWT配置参数

### 配置变更

- 新增JWT签名密钥配置
- 新增Token过期时间配置(15分钟)
- 新增黑名单Redis key配置

### 数据库变更

- 新增表: `token_blacklist`(Token黑名单)
- 无删除表

## 变更分析

### 变更原因

1. Session方案水平扩展能力受限
2. 多端统一认证需求
3. 减轻Redis共享压力

### 变更影响

**正面影响**:
- 支持无状态水平扩展
- 多端认证统一
- 降低Redis依赖

**潜在影响**:
- Token主动注销需黑名单机制
- 短Token需刷新机制
- 现有调用方需适配

## 影响面分析

### 上游影响

- API网关: 需适配JWT传递方式
- BFF层: 需调整认证头处理
- 各端SDK: 需适配Token存储和刷新

### 下游影响

- 权限服务: 无影响,权限校验逻辑不变
- 消息服务: 无影响

### 现有功能影响

- 登录流程: 接口返回值变更
- 注销流程: 需调用黑名单接口
- Token校验: Header名称从`X-Session-Id`改为`Authorization`

### 兼容性风险

- 现有客户端需同步升级
- 建议灰度期间Session和JWT并存,逐步迁移

## 风险评估

**风险等级**: 中等

**风险点**:
1. 客户端升级协调成本
2. 黑名单机制增加Redis压力
3. Token刷新机制复杂度

**缓解措施**:
1. 灰度发布,先内部系统,再外部客户端
2. 黑名单仅存储未过期Token,设置自动过期
3. 编写详细的Token刷新文档和SDK

## 验证方案

### 单元测试

- JWT生成和验证逻辑测试
- 黑名单机制测试
- Token刷新逻辑测试

### 集成测试

- 登录流程端到端测试
- Token校验链路测试
- 注销后Token失效测试

### 性能测试

- 登录接口QPS测试(目标提升50%)
- Token校验延迟测试(目标<10ms)

### 灰度验证

- 内部系统灰度1周,观察监控
- 外部客户端逐步升级

## 回滚方案

详见: [[0001_auth-module-rollback]]

**快速回滚**:
1. 保留Session分支,可快速切回
2. 数据库变更可逆(删除`token_blacklist`表)
3. 配置开关控制认证方式

## 相关页面

- [[0001_user-center-auth-refactor]] - 所属需求
- [[0001_auth-solution-decision]] - 技术决策依据
- [[0001_auth-module-rollback]] - 回滚详细方案
```

---

### Insight 页面模板 (洞察/决策记录/踩坑总结)

用于架构决策记录(ADR)、技术选型结论、踩坑总结、方案对比等持久结论。

**适用场景**:
- 某个结果不应消失在聊天记录中
- 某个对比或综合分析未来可能复用
- 某次讨论实质性地改变了当前对某主题的理解

**Frontmatter 结构**:

```yaml
---
type: insight
title: 认证方案选型结论
slug: 0001_auth-solution-decision
tags:
  - auth
  - architecture
status: active
created: 2026-05-11
updated: 2026-05-11
confidence: high
sources:
  - path: raw/sources/auth-comparison-analysis.md
    date: 2026-05-11
derived_from:
  - wiki/stories/0001_user-center-auth-refactor.md
  - wiki/concepts/0001_oauth2-jwt-pattern.md
related_stories:
  - wiki/stories/0001_user-center-auth-refactor.md
supersedes:
  - wiki/insights/0001_session-based-auth-conclusion.md
superseded_by: []
---
```

**字段说明**:
- `confidence`: 结论当前的确定程度,使用 `low`、`medium` 或 `high`
- `derived_from`: 产出此洞察的直接上游页面或原始快照
- `related_stories`: 审查或检索时应呈现此洞察的需求/故事
- `supersedes` 和 `superseded_by`: 仅当一个洞察明确取代或修正另一个时使用

**正文结构**:

```markdown
# 认证方案选型结论

## 摘要

对于当前用户中心场景,选择JWT无状态认证而非Session方案。

## 决策内容

**选型**: JWT + Redis黑名单方案

**核心理由**:
1. 无状态特性天然支持水平扩展
2. 多端统一认证成本更低
3. 符合微服务架构趋势

## 备选方案对比

| 方案 | 优势 | 劣势 |
|-----|------|------|
| Session + Redis共享 | 主动注销即时生效 | Redis压力、序列化开销 |
| JWT + 黑名单 | 无状态、易扩展 | 主动注销需黑名单机制 |
| OAuth2授权码 | 标准化、第三方友好 | 实现复杂、过度设计 |

## 适用边界

- 本结论适用于: 内部系统、可控网络环境、用户规模 < 1000万
- 不适用于: 强即时注销要求、跨域第三方集成场景

## 风险与缓解

- 风险: JWT无法主动作废
- 缓解: Redis黑名单 + 短过期时间(15分钟) + 刷新Token机制

## 相关页面

- [[0001_user-center-auth-refactor]] - 应用此决策的需求
- [[0001_oauth2-jwt-pattern]] - 相关技术概念
```

---

### Candidate 页面模板 (候选/待验证方案)

用于待验证的方案草稿、尚不稳定的临时笔记,后续可提升、合并或丢弃。

**生命周期**: `open -> promoted | merged | dropped`

**适用场景**:
- 对话产出了有前景的假设、重构或开放问题,有未来复用价值
- 材料明显与现有主题相关,但仍需验证、综合或清理
- 直接写入 `concept`、`entity`、`project` 或 `insight` 为时尚早

**不适用场景**: 个人笔记、原始聊天记录、一次性任务跟踪。

**Frontmatter 结构**:

```yaml
---
type: candidate
title: 候选: 灰度发布采用金丝雀策略
slug: 0001_candidate-canary-release-strategy
status: open
created: 2026-05-11
updated: 2026-05-11
related_topic: 0001_user-center-auth-refactor
derived_from:
  - wiki/stories/0001_user-center-auth-refactor.md
  - raw/sources/release-strategy-research.md
why_it_matters: 这将影响用户中心重构的发布计划和回滚机制。
next_action: 验证金丝雀与当前基础设施的兼容性,然后提升为正式决策或丢弃。
---
```

**决议后添加**:

```yaml
resolved_at: 2026-05-15
resolution_target: wiki/insights/0001_release-strategy-decision.md
resolution_note: 验证后确认为金丝雀策略,提升为正式决策。
```

**字段说明**:
- `status`: 使用 `open`、`promoted`、`merged` 或 `dropped`
- `resolved_at`: 候选不再为 `open` 时添加
- `resolution_target`: 吸收或替代该候选的页面路径; `dropped` 时可省略
- `resolution_note`: 一句简短说明为何提升、合并或丢弃
- `related_topic`: 一个主要主题或页面路径;次要链接放正文
- `derived_from`: 触发此笔记的直接来源或页面路径,不是原始聊天
- `why_it_matters`: 一句说明为何有复用价值
- `next_action`: 可解决或推进该笔记的最小下一步;主要用于 `open` 候选
- `updated`: 笔记变更或添加决议元数据时刷新,便于识别过期候选

**正文结构**:

```markdown
# 候选: 灰度发布采用金丝雀策略

## 摘要

初步倾向采用金丝雀发布策略进行用户中心重构的灰度上线。

## 暂定理由

- 当前K8s基础设施支持Istio流量切分
- 金丝雀可精确控制流量比例,便于观察
- 回滚成本低,只需调整流量权重

## 待验证事项

- 与现有监控告警体系的集成
- 多服务间调用链的金丝雀协同

## 相关页面

- [[0001_user-center-auth-refactor]] - 可能影响的需求
- [[0001_release-strategy-decision]] - 验证后的可能目标页面

## 决议

(候选解决后填写)

- 结果: promoted
- 目标: [[0001_release-strategy-decision]]
- 理由: 验证通过,确认金丝雀策略可行
```

## 知识边界

优先存储:

- 架构决策及其理由(为什么选 A 不选 B)
- 技术选型结论与对比分析
- 重要的踩坑经验和已验证的死胡同
- 服务、中间件、依赖的配置信息与使用约定
- 项目当前方向、开放问题与待办事项
- 编码规范、接口约定等可复用的稳定知识

不优先存储:

- 日常 bug 修复的细节(除非包含可复用的经验)
- 临时调试日志
- 聊天记录原文
- 个人偏好或风格相关内容

## 对话式使用

用户在普通的 agent 对话中工作即可。

不需要平台特定的斜杠命令、按钮、单独的知识管理模式、后台服务或数据库。
当用户用自然语言表达明确意图时,将其识别为工作流触发器。
协议术语如"候选"、"洞察"、"提升"、"合并"是内部标签,用户不需要掌握。
优先使用当前对话的语言。语言不明时,回退到用户的系统语言。

## 自主策略

采用保守的自主边界:

- 在会话开始、续接、上下文恢复、检索和整理检查时主动读取。包括 `wiki/active.md`、`wiki/recent.md`、`wiki/index.md`、相关正式页面、需要的候选笔记、以及 `meta/lint-status.json`。
- 用户未表达记录意图时只建议不写入。可以建议收录、候选捕获、提升、合并、清理或整理,但先不修改知识库。
- 用户用自然语言明确表达记录意图时视为写入授权。如"把这个方案记下来"、"先存着这个想法"、"这个方案确定了,正式记下来"、"合并到之前那个模块里"、"整理一下项目知识"等。
- 高影响操作前必须确认:删除或归档页面、批量清理、大规模重构、目录结构变更。
- 默认保守:不把普通聊天写入知识库,不把候选笔记当作正式知识,不把讨论链接等同于收录。

## 自然语言意图映射

以下为意图族,不需要精确匹配。只有当用户的措辞明确表达了存储、决策记录或维护意图时才触发:

- "收录"、"把这个需求文档收进来"、"把这段设计记下来":执行收录工作流
- "继续上次那个模块的讨论"、"之前架构评审定了什么"、"我们之前怎么决定用XX的":查询知识库并继续相关线索
- "这个方案先存着"、"先别当正式结论"、"记一下这个思路":创建或更新候选笔记
- "这个方案确定了"、"正式记下来"、"把这个决策记到ADR里":执行候选提升或其他持久化回写
- "把这个并到用户中心模块里"、"合并到之前那个架构决策里":执行候选或页面合并工作流
- "这个方案不成立了"、"去掉这个暂存方案":执行候选丢弃工作流
- "整理一下项目知识"、"做一轮检查"、"清理一下":执行整理工作流
- "看看知识库有没有过时的"、"项目文档健不健康"、"有没有该更新的":执行健康检查

## 默认执行与澄清

当用户请求明确暗示续接或知识查询时,在当前对话中直接执行读取并回答。
当用户请求明确表达收录、候选捕获、决策记录、回写或维护意图时,直接执行写入工作流并简要汇报。
不要把普通分析、总结或链接讨论变成文件写入,除非用户要求保留、添加、存储、提升、合并、丢弃或维护。
一旦授权工作流明确,不要在每个中间步骤停下来请求许可。

仅在以下情况问一个简短的澄清问题:

- 多个主题、候选或目标页面都可能适用
- 不确定用户只要分析还是要写入
- 请求涉及删除、归档、批量清理、大规模重构或其他高影响操作

## 会话开始

1. 读取 `meta/lint-status.json`。
2. 如果 `last_lint` 超过 24 小时,提醒用户。
3. 如果用户开始项目讨论、续接、技术选型、架构评审或计划任务,先按检索顺序查询知识库,再做广泛外部搜索。
4. 保持知识库提示简短,不要打断简单的语法、API、执行或调试任务。

## 何时查询知识库

主动查询知识库的场景:

- 用户在继续之前的模块、项目或开放问题
- 用户要求对比技术方案、做选型建议、权衡分析、路线图规划
- 用户分享的新材料与已有主题、项目或决策重叠
- 问题属于持续的项目线索
- 历史决策、踩坑经验或原始来源会显著提升回答质量
- 即将给出项目级别的结论或计划

不优先查询知识库的场景:

- 简单的语法或 API 用法问题
- 当前仓库、终端、日志或运行时的即时调试
- 一次性执行任务,没有持续的项目上下文
- 答案主要依赖与项目历史无关的实时信息

## 检索顺序

知识库查询触发时,按以下顺序读取,获得足够上下文后停止:

1. `wiki/active.md`
2. `wiki/recent.md`
3. `wiki/index.md`
4. 相关 `wiki/stories/*.md`
5. 相关 `wiki/changes/*.md`
6. 相关 `wiki/insights/*.md`
7. 相关 `wiki/concepts/*.md` 和 `wiki/entities/*.md`
8. 相关 `meta/candidates/*.md`(仅在正式 wiki 不够时补充)

入口页面优先,详细页面其次。除非入口页面无法缩小范围,否则不要直接跳到广泛搜索。

## 收录触发

当用户分享以下内容时主动提供收录:

- 需求文档、设计稿、架构图、接口文档、会议纪要
- 大段粘贴的方案说明或技术调研笔记
- 应该纳入项目长期上下文的材料

不自动收录:

- 仅用于即时排障的 Stack Overflow 链接或报错页面
- 无长期价值的临时链接
- 一次性日志、草稿或执行追踪

对链接做总结、翻译或提取要点不等于收录。只有用户要求记录以备后用时才捕获。

## 收录工作流

用户明确要求收录、记录或保存时执行:

1. 读取原始材料。
2. 提取 3-5 个关键要点。
3. 仅当材料重要且强调方向影响路由、或收录范围确实模糊时,与用户确认要点。
4. 在 `raw/sources/` 保存原始快照。
5. 将稳定内容路由到正确页面类型:
   - `concept`:架构概念、技术规范、设计模式、编码约定
   - `entity`:服务、中间件、数据库、第三方依赖、团队角色
   - `story`:需求、用户故事、功能改进、缺陷修复
   - `change`:代码变更记录、变更分析、影响面评估
   - `insight`:架构决策记录(ADR)、选型结论、踩坑总结
6. 如果对话中同时产生了有价值但尚未稳定的成果,捕获到 `meta/candidates/` 而非强行写入正式 wiki。
7. 在 `wiki/concepts/`、`wiki/entities/`、`wiki/stories/`、`wiki/changes/`、`wiki/insights/` 中创建或更新页面。
8. 遵循 `meta/schemas/` 中的页面结构。
9. 添加或刷新交叉链接。
10. 按需更新 `wiki/active.md`、`wiki/recent.md`、`wiki/index.md`、`meta/candidates/index.md`、`wiki/log.md` 和 `meta/lint-status.json`。

## 查询工作流

1. 知识库查询触发时,按检索顺序读取。
2. 先读直接相关的页面,再回退到原始来源。
3. `meta/candidates/` 仅作为补充线索:
   - 提示值得验证的暂定假设
   - 恢复最近但未解决的工作
   - 指向需要确认的页面、来源或开放问题
4. 回答时附带明确的文件引用。
5. 将稳定的 wiki 结论与暂定的候选线索区分开。
6. 不要把候选笔记作为最终结论的唯一依据,除非用户明确要求查看暂定材料。
7. 如果用户请求已暗示回写、提升、合并、丢弃或维护,直接执行。否则建议相关回写而不是自动持久化。

## 候选规则

`meta/candidates/` 是有价值但尚不够稳定进入正式 wiki 的缓冲区。

使用候选笔记的场景:

- 方案可能改变需求方向或重新定义开放问题
- 有明确的未来复用价值但尚需验证或整合
- 可与已有的来源、故事、变更、概念、实体或洞察关联

不使用候选笔记的场景:

- 个人笔记
- 无复用结构的随意猜测
- 临时任务跟踪
- 原始对话记录

候选不是一等真相源。它们是可能被提升、合并或丢弃的工作材料。

一次强有力的对话观察本身不足以创建候选笔记。除非用户要求保留,先建议候选捕获。

创建新候选前,先检查 `meta/candidates/index.md` 和相关开放笔记。当新材料属于同一未决线索时,优先更新已有候选。

默认候选审查规则:

- 在以下时机审查 `open` 候选:用于回答时、相关正式页面变更时、整理时、或创建/更新后不超过 7 天
- 无审查、更新、提升、合并或丢弃的 `open` 候选在 14 天后视为过期
- 过期不等于自动删除;表示下次整理应解决该笔记,或如果仍有复用价值则刷新 `updated` 并使 `next_action` 更具体

## 候选决议工作流

决议候选时:

- `promote`:将笔记转为新的或实质修订的正式页面(通常为 `insight`),候选状态置为 `promoted`
- `merge`:将持久部分合并到已有的 `story`、`change`、`insight`、`concept` 或 `entity` 页面,候选状态置为 `merged`
- `drop`:当笔记不再有长期复用价值时,状态置为 `dropped`

决议更新要求:

- 始终更新候选笔记状态和 `updated`
- 始终刷新 `meta/candidates/index.md`
- `promote` 或 `merge` 时更新目标正式页面
- `promote` 或 `merge` 后刷新 `wiki/recent.md`,`drop` 仅在活跃工作被实质改变时刷新
- 新增页面或可发现性链接变更时刷新 `wiki/index.md`
- 结果改变了当前方向、活跃需求或开放问题时刷新 `wiki/active.md`
- 在 `wiki/log.md` 追加简短的维护记录
- 更新 `meta/lint-status.json` 反映维护操作

## 回写规则

仅在用户要求记录结果或请求已包含明确写入授权时执行回写。否则简要建议回写,保持知识库不变。

只回写持久知识,例如:

- 有力的技术方案对比或综合分析
- 稳定的架构概念、技术规范或编码约定
- 项目方向、技术选型理由、开放问题、或已验证的死胡同
- 架构决策记录或可复用的踩坑经验
- 可能帮助未来工作的澄清

如果材料有前景但仍不稳定,优先使用候选笔记而非污染正式 wiki 页面。

不回写:

- 日常闲聊
- 临时任务状态
- 无明确复用价值的临时猜测
- 应作为已有页面更新的重复内容

优先更新已有页面而非创建近似重复。
拿不定时,创建或更新 `insight` 页面,或使用候选笔记,而不是存储整个对话记录。

## 整理工作流

主动读取 `meta/lint-status.json`。

建议整理的时机:

- `last_lint` 超过 24 小时
- 用户分享的工作可能很快受益于一次维护

用户明确要求维护、健康检查、过期候选审查或请求明确包含维护意图时执行整理,尤其是在以下场景:

- 批量收录或多个正式页面更新后
- 创建、决议或积累多个候选后
- 依赖较旧的项目或洞察页面做新的项目级综合、建议或路线图之前

定期检查知识库:

- 页面之间的矛盾
- 被更新来源取代的过时结论
- 无有效入站引用的孤立页面
- 反复出现但缺少页面的概念、实体、故事、变更或洞察
- 缺失的交叉链接
- 索引不匹配
- 合并重复页面的机会
- 应提升、合并或丢弃的过期候选

整理时,将 `meta/candidates/index.md` 和开放候选作为显式维护队列而非永久积压。
如果候选过期,默认在该轮解决。仅当仍有明确复用价值时保持开放,并刷新 `updated`、使 `next_action` 更具体。

删除、归档变更、批量清理、大规模结构变更或目录变更前必须询问用户。

## 页面类型与项目管理的对应

| 页面类型 | 项目管理用途 | 典型内容 |
|---------|------------|---------|
| `concept` | 架构概念、技术规范 | 微服务拆分原则、缓存策略、统一异常处理规范、接口版本管理约定 |
| `entity` | 服务、中间件、依赖 | 用户中心服务、认证网关、Redis集群、Kafka、XX第三方SDK |
| `story` | 需求、用户故事 | 认证重构需求、订单金额计算修复、登录接口性能优化 |
| `change` | 代码变更记录 | 认证模块重构变更、网关集成变更、数据库迁移变更 |
| `insight` | 架构决策记录、踩坑总结 | ADR: 选JWT而非Session、缓存击穿防护实践、分库分表策略决策 |
| `candidate` | 待验证方案 | 异步消息可靠性方案(事务消息 vs 本地消息表)、灰度发布策略 |

## 依据规则

- 不编造知识库或明确外部来源不支持的知识
- 保持 `raw/` 仅追加
- 尽可能保持来源引用明确
- 标注矛盾和不确定性而非隐藏
- 不把候选笔记等同于正式 wiki 页面或原始快照
- 用户想保留的好答案不应消失在聊天记录中

## 文件约定

- 文件名使用四位数字序号前缀，格式为 `NNNN_slug.md`（如 `0001_sql-scan-engine.md`）
- 序号按 index.md 中排列顺序分配，便于在文件目录中快速定位
- 使用小写、连字符分隔的 slug
- 页面标题保持人类可读
- 每个页面顶部放简短摘要
- 交叉链接保持明确
- 优先使用纯 markdown 和简单 frontmatter

## 元数据更新

- 时间字段使用 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SSZ` 或带显式 UTC 偏移的 ISO 8601 格式
- 新增原始材料时更新 `last_ingest`
- 从查询、综合、提升或合并回写持久知识时更新 `last_writeback`
- 每次整理或候选清理时更新 `last_lint`、递增 `lint_count`、尽可能重计 `total_pages` 和 `total_sources`
- 在 `wiki/log.md` 中记录维护结果,包含日期和具体操作,尤其是候选审查和 `promote` / `merge` / `drop` 结果

如果平台没有持久化指令面,将本文件本身作为每次会话的启动协议。
