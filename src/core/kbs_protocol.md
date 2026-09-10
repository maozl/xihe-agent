# 业务知识库维护协议（精简版）

维护一个按业务域组织的工作知识库。**按需启用**——不开启时无任何影响。
根目录 `<root>`，以下路径均相对它。

## 工具

- 建空库：`kbs_init`（仅当 `<root>` 缺失/为空时；首次调用前征得用户确认）
- 会话启动健康检查：`kbs_status`
- 查询定位：`kbs_search`（机制见其工具说明）
- 读写知识库文件：`read_file` / `write_file` / `search_files` / `patch`（操作限在 `<root>` 下；读已定位的具体文件用 `read_file`）

## 知识模型

- `raw/sources/` 原始材料快照（**仅追加**，不可改）
- `wiki/active.md`（当前工作/待决）、`recent.md`（最近变更）、`index.md`（目录）、`log.md`（维护日志）
- `wiki/domains/index.md` **业务域受控词表，唯一事实源**；`domains/<slug>.md` 各域中心页
- `wiki/concepts/`、`wiki/insights/` 扁平，带 `domain` 字段
- `wiki/entities/<domain>/` 实体按主域分子目录；`_shared/` = 跨域
- `meta/candidates/` 暂存待验证笔记（**非一等真相源**）；`meta/schemas/` 页面模板；`meta/lint-status.json` 健康度

## 归属规则

- `domain` 是受控词表，不是自由标签；每个值必须出现在 `wiki/domains/index.md`。
- `domain` 多值；**第一个是主域**，决定实体物理目录。跨域页放主域目录，但须从涉及的每个域中心引用。
- Slug 小写、连字符、英文（如 `data-governance`）。

## 意图识别（写入授权的判定）

仅当用户措辞**明确表达存储/决议/维护**意图时才写入；普通分析、总结、翻译链接**不**触发写入。意图族 → 工作流（执行用文件工具 + `<root>/AGENT.md` 对应步骤）：

| 用户意图（示例措辞） | 触发工作流 |
|---|---|
| "把这个链接/文章收录""存起来以后用""记进知识库" | 收录（raw 快照 + 路由到 entity/concept/insight） |
| "先记一下""先存着""先别当正式结论""以后可能有用" | 候选（`meta/candidates/`） |
| "已经比较确定了""正式记下来""整理成正式结论" | 候选提升 / 正式回写 |
| "并到数据治理那条线""合并到之前那条结论" | 候选或页面合并 |
| "这个不成立了""把这条去掉" | 候选丢弃（若暗示更大清理，先问） |
| "整理一下知识库""过一遍""健不健康" | 整理 / 健康检查（先 `kbs_status`） |
| "继续上次聊的""之前研究过什么" | 查询（只读） |

映射原则跟随用户当前语言，不限于中文。

## 自主边界

- 用户**未表达记录意图 → 只建议，不写入**。
- 写入授权一旦明确，**不要在每个中间步骤停下来再问**。
- **结构性变更必须先确认**：创建/合并/拆分/退役域、删除或归档页、批量清理、目录重组、领域级变更。
- Slug 漂移（错字/同义词/过期 slug）属内容修正，整理时直接改，不用问。
- 候选笔记 ≠ 正式知识；勿拿候选作唯一结论依据。

## 写入纪律（执行任何写入前必做）

1. **先读规范**：`read_file` 读 `<root>/AGENT.md` 对应工作流章节 + `<root>/meta/schemas/<类型>.md`，严格按其步骤与 frontmatter——**不凭记忆写**。
2. **溯源**：能用 `sources` 字段回溯到 `raw/sources/` 的就标；`raw/` 仅追加。
3. **保持账本同步**：写入后按 AGENT.md「元数据更新」刷新——至少 `wiki/recent.md`、`wiki/log.md`、`meta/lint-status.json`；新增/移动页面时刷 `wiki/index.md` 与所涉 `domains/<slug>.md` 中心范围；结果改变活跃论点时刷 `wiki/active.md`。
4. 标注矛盾而非隐藏；不编造。

## 会话启动

用户在续接/研究/对比/主题级规划时：先调 `kbs_status` 看健康度并按"检索顺序"读知识库，再外部搜索。简单语法/API/即时调试不必查。缺库时按 `kbs_init` 规则建库再继续。

## 检索纪律

查询走 `kbs_search`，不对 `<root>` 直接 grep；未命中才用 `search_files` 兜底。主题级综合前按需顺读：`active.md` → `recent.md` → 相关 `domains/<slug>.md` → 相关 `insights/` → `candidates/`（仅补充）。

## 候选生命周期（简）

`open → promoted | merged | dropped`。`open` 候选 14 天无动作视为过期（整理时解决或刷新）。
