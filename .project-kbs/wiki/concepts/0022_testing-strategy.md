---
type: concept
title: 测试策略——分层模型
slug: 0022_testing-strategy
aliases:
  - 测试方案
  - 测试分层
tags:
  - testing
  - quality
status: active
created: 2026-08-07
updated: 2026-08-07
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0016_interrupt-stop-steer.md
  - wiki/entities/0001_xihe-agent.md
sources:
  - path: (对话产出) agent 测试方法讨论 + tests/ 落地实现
    date: 2026-08-07
---

# 测试策略——分层模型

## 摘要

xihe-agent 是"非确定性 LLM + 长链路工具调用 + 强外部依赖(模型 API/浏览器/shell/平台 webhook)"的系统,传统 `assertEqual` 几乎无处下手。本页定义 xihe 的测试策略:**按保真度分层,每层用不同判据**。核心技巧是用一个**注入的假模型 client** 把 agent 循环变成确定性的,从而用普通断言测循环不变量(撞墙、派工、崩溃恢复)。截至 2026-08-07,L0/L1/L2 的最小骨架已落地于 `tests/`(12 个测试全绿);L3/L4 为后续方向。

## 为什么 agent 难测 / 为什么分层

LLM 输出非确定、链路长、外部依赖重。单个"端到端跑真模型"的测试又慢、又贵、又 flaky。所以正解不是用一种策略测整个 agent,而是按保真度拆层:底层快而确定(覆盖纯逻辑),高层慢而非确定(只测少量关键任务)。

## 分层模型(对照 xihe 代码)

| 层 | 测什么 | 判据 | 成本 | xihe 对应(已落地 → ✅) |
|---|---|---|---|---|
| **L0 纯函数** | 无 IO 的确定性逻辑 | 普通断言 | 极低/快/零 API | ✅ `tests/test_ascii_safe.py`(`_ascii_safe`)、`tests/test_dangerous_command.py`(`_detect_dangerous_command`) |
| **L1 工具 handler** | 单个 tool 入参→JSON | 结构断言(mock IO) | 低/快 | ✅ `tests/test_terminal_tool.py`(monkeypatch `subprocess.Popen`,断言截断/退出码解释) |
| **L2 agent 循环 + 假模型** | 循环不变量 | 行为断言 | 中/快/零 API | ✅ `tests/test_agent_loop.py`(`FakeChatClient` 驱动 `chat()`) |
| **L3 真 LLM + judge** | 任务是否达成 | LLM-as-judge + 结构断言 | 高/慢/花钱 | ⏳ 后续(复用 `core/auxiliary_client.py` 当 judge,nightly 套件) |
| **L4 平台集成** | gateway 全链路 | 消息级断言 | 中 | ⏳ 后续(假平台 server + `BasePlatformAdapter`) |

**关键事实**:xihe 的 agent 循环很薄(`core/agent.py` 的 `chat()`),逻辑大半在 tool handler 里。所以 L0+L1 覆盖 ~80% 回归面且完全确定、零成本——是地基。

## 关键技巧——mock 模型 client(L2 的核心)

agent 循环的不变量(撞 `max_iterations`、并行 vs 串行派工、崩溃恢复)无法用真模型稳定复现。解法:**注入一个假 client**,按剧本返回 tool_call / 最终文本,让循环变成确定性的。

xihe 的落地点(`core/agent.py` `XiheAgent.__init__`):

```python
def __init__(self, config=None, *, ..., client=None):
    self.client = client or OpenAI(...)   # 默认建真 client;测试传假 client
```

- **向后兼容**:`client=None` 时行为与改动前完全一致(CLI/gateway 不传 client)。这是**唯一**为可测性做的生产代码改动,一行级。
- **`tests/fakes.py` 的 `FakeChatClient`** 用 `types.SimpleNamespace` 仿造 OpenAI SDK 的对象形状(已对照 `_non_streaming_call`/`_streaming_call`/`_record_usage` 逐属性确认),零 SDK 依赖、零网络。`script` 参数是"每轮弹一个预置响应"的剧本;`never_finish=True` 让循环只发 tool_call、永不发最终内容,必然撞 `iter_limit`。

示例(直接保护 `_last_exit_reason` 改动):

```python
client = FakeChatClient(never_finish=True)
agent = make_agent(client)
result = agent.chat(source=src, user_message="loop forever", max_iterations=2)
assert agent._last_exit_reason == "max_iterations"
assert "单轮处理上限" in result
```

### 测试隔离约定

- **会话 DB 隔离**:`SessionDB` 的路径取自模块级常量 `core.session._DB_PATH`(导入时由 `AGENT_HOME` 算出,项目 `agent_home: .xihe-agent` → `<repo>/.xihe-agent`)。`tests/conftest.py` 的 `isolate_db`(autouse)把该常量 monkeypatch 到 `tmp_path`,测试不碰真实 session store。
- **跳过副作用**:`make_agent` 默认 `is_subagent=True`(跳过 auto-title,否则会调 aux LLM)+ `system_prompt_override`(短路 `_build_system_prompt`,不加载 skills/kbs/项目上下文)→ L2 测试 hermetic 且快。
- **工具隔离**:`load_all_tools()` 在 `core.agent` 导入时跑(注册全部真工具),但 `FakeChatClient` 只请求测试注册的 `test_echo`,真工具在 schema 里是惰性的,不影响断言。

## 行业对照(为什么这套是主流)

这套手法与 agent 测试行业实践一致,非生造:
- **mock/注入模型 client**:LangChain `FakeListChatModel`、OpenAI SDK 的 responses 回放等同此思路。
- **LLM-as-judge**(L3):G-Eval、DeepEval、Ragas、Promptfoo、LangSmith 全建立在此。
- agent 比普通软件多两块重点(本骨架暂未做,记为后续):**轨迹评估**(评整条步骤链而非只看最终答案)、**沙箱+任务+scorer**(给真实环境+任务,scorer 查环境最终状态判对错,如 SWE-bench 跑仓库测试套件——判据确定,是端到端金标准)。

## 务实路径

1. ✅ **L0/L1/L2 地基**(本次落地):pytest + 可注入 client + `FakeChatClient` + 12 个代表性测试。每层 1–2 个,给模式,增量覆盖留给后续。
2. ⏳ **L3 nightly**:真模型 + `auxiliary_client` 当 judge + 结构断言("至少调了某工具""输出合法 JSON""< N 迭代")。不进阻塞 CI。
3. ⏳ **L4 平台集成**:等 WeCom/Feishu 协议稳定后,用假平台 server 测 `handle_message` 全链路。

## 命名/目录约定

- `tests/test_*.py`,一文件一模块(对照被测源文件)。
- 共享 fixture 与假对象放 `tests/conftest.py` 与 `tests/fakes.py`。
- 新增工具时,至少补一条 L1(mock IO,断言输出 shaping);改循环/退出语义时,补 L2(`FakeChatClient` 剧本)。

## 已知扩展点(骨架有意未做)

- **并行 vs 串行派工**(`read_only=True` → `ThreadPoolExecutor`):基于时序的断言易 flaky,骨架里 L2 只验证"一轮多个 tool_call 都被派发并组装结果"这条功能不变量;真正的并发性留作带线程观测的专项测试。
- **崩溃恢复**(`_repair_dangling_tool_calls`):可加一个"假 client 发了 tool_call 后抛异常"的剧本,断言悬空 tool_call 被补成 error——后续补。
- **流式路径**:`FakeChatClient` 已支持 `stream=True`,但骨架的 L2 测试走非流式(更快);流式往返可按需补。

## 验证

- `pip install -e .`(已加 `pytest>=7.0`)后 `pytest -q` → 12 绿。
- 有效性自证:`test_max_iterations_sets_exit_reason` 断言 `agent._last_exit_reason == "max_iterations"`;该属性在 `__init__` 默认 `None`、仅撞墙返回路径设置——测试通过即证明该路径生效,移除设置则转红(非空转)。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — 工具注册/dispatch/check_fn(L1 测的就是 handler,L2 测的就是 dispatch 链)
- [[0016_interrupt-stop-steer]] — 循环控制通道(中断/steer 是 L2 该覆盖的循环不变量)
- [[0001_xihe-agent]] — 项目总览
