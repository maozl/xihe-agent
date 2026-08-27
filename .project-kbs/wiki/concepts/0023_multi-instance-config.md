---
type: concept
title: 多实例配置——--config 启动时选实例
slug: 0023_multi-instance-config
aliases:
  - --config
  - 实例配置
  - 多 xihe 隔离
tags:
  - config
  - deployment
status: active
created: 2026-08-07
updated: 2026-08-17
related_pages:
  - wiki/concepts/0006_session-design.md
  - wiki/concepts/0022_testing-strategy.md
  - wiki/entities/0001_xihe-agent.md
sources:
  - path: (对话产出) 多实例配置设计讨论 + src/core/config.py 落地
    date: 2026-08-07
---

# 多实例配置——`--config` 启动时选实例

> **⚠️ 分层/`.env` 部分已过时（2026-08-13 单源化，2026-08-17 订正）**：下文「优先级 5 层表」「.env 跟随与浮顶陷阱」两节、示例中 `secret: ${WECOM_SECRET}`、数据根链里的「项目 config.yaml.agent_home」层，均为**单源化前形态**。现行为：全部配置在**一个** config.yaml（`--config` 指定的实例文件，或默认 `~/.xihe-agent/config.yaml`），值全字面——**无 `.env`、无环境变量覆盖、无 `${VAR}` 展开**，7 个 env-aliased key 的抢占问题不复存在（凭据直接写字面值）；数据根优先级 `~/.xihe-agent` < `AGENT_HOME` < `--config` 文件的 `agent_home`（例外：`agent_home` 定位器值本身支持 `${VAR}` 展开）。**本文仍有效**：`--config` 机制本身、peek argv 设计决策（0 下游重构）、`agent_home` 数据隔离边界、`XIHE_CONFIG_FILE` 导出。单源语义详见 [[0001_xihe-agent]] 配置单源条目。

## 摘要

`xihe --config path/to/x.yaml` 让**一个 YAML 文件完整描述一个"实例"**：`agent_home` 决定数据根（`sessions.db` / `agent.log` / `browser` / `cron` 各自隔离），其余键作为最高优先级配置覆盖。目的：同台机器**同时跑多个 xihe、配置与数据互不干扰**（一个接 WeCom、一个接 Feishu，或对接不同模型）。比 `--profile` 更透明（无魔法命名 / 约定目录）。2026-08-07 落地于 `src/core/config.py` + `src/cli/app.py` + `src/cli/chat.py` + `src/gateway/bot.py`。

## 使用方法

```bash
# 两个实例，配置与数据完全隔离，可并存
xihe --config configs/wecom-prod.yaml gateway
xihe --config configs/feishu-prod.yaml gateway

# CLI / cron 同样支持
xihe --config configs/dev.yaml chat -q "hi"
xihe --config configs/dev.yaml cron list
```

实例 YAML 示例（`configs/wecom-prod.yaml`）：

```yaml
agent_home: .xihe-wecom        # → 数据根（sessions/log/browser/cron 落这里）
model: glm-5.2-zp
platform: wecom
platforms:
  wecom:
    secret: ${WECOM_SECRET}     # 真值放 configs/.env（sibling，见"陷阱"）
```

- `--config` **位置自由**：`xihe --config x.yaml chat` 与 `xihe chat --config x.yaml` 都认（顶层 + 各 subparser 都注册，subparser 用 `argparse.SUPPRESS` 避免覆盖顶层解析值）。
- `agent_home` 相对路径相对**仓库根**（与项目 `config.yaml` 一致）；跨仓库使用建议写绝对路径。

## 优先级（低 → 高）

`load_config()` 的完整配置优先级：

| 层 | 来源 | 备注 |
|---|---|---|
| 1 | 内置默认 | |
| 2 | 用户 `~/.xihe-agent/config.yaml`（即当前 `AGENT_HOME`） | |
| 3 | 项目 `config.yaml`（仓库根） | |
| 4 | **`--config` 指定 YAML**（最高优先 YAML） | 本次新增 |
| 5 | `.env` / 环境变量（整体压在所有 YAML 之上） | 见"陷阱" |

`agent_home`（数据根）优先级**独立**解析（`_resolve_agent_home`）：

```
~/.xihe-agent  <  项目 config.yaml.agent_home  <  AGENT_HOME 环境变量  <  --config YAML.agent_home
```

## 设计方案——peek argv（为什么不动 13 个常量）

**核心矛盾**：`AGENT_HOME` 在 import 时就算定（`config.py` module-level `AGENT_HOME = _resolve_agent_home()`），早于 `main()` / argparse；而它被 **13 个 module-level 常量跨 11 个文件**捕获（`session._DB_PATH`、`browser._STATES_DIR`、`cron._CRON_DIR`、`chat._HIST_PATH`、`memory._MEMORIES_FILE`、`todo._TODO_FILE`、`skills._USER_SKILLS_DIR`、`ssh._SSH_DIR`、`platforms._IMAGE_CACHE_DIR`…）。

**两条路（已决策）**：

- **A. peek argv（采纳）**：`config.py` 在 `_resolve_agent_home()` 里 peek `sys.argv` 的 `--config`，import 时（`sys.argv` 在任何用户代码前已就绪）就把 `AGENT_HOME` 算对 → 下游 13 个常量自动正确，**零重构**。
- **B. 函数化 `AGENT_HOME`（否决）**：去 import 副作用 + `paths.xxx()` 访问器 → ~30 touch-point（17 import + 13 常量 + `conftest` fixture + 本 wiki），漏改一处静默写错路径，风险高。

选 A 的理由：peek argv 与读 `AGENT_HOME` env **同质**（都是 launch-time 来源，进程启动前已就绪），是合法设计非 hack；`pytest` / `pip` 等均用此模式做早期决策。

**关键代码**（`src/core/config.py`）：

```python
def _peek_config_flag() -> Path | None:
    """扫 sys.argv 的 --config（容忍 `--config x` / `--config=x`，跳过 `--config -flag`）。"""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return Path(argv[i + 1]).expanduser().resolve()
        if a.startswith("--config="):
            return Path(a[len("--config="):]).expanduser().resolve()
    return None

def _resolve_agent_home() -> Path:
    cli_cfg = _peek_config_flag()
    if cli_cfg:
        os.environ["XIHE_CONFIG_FILE"] = str(cli_cfg)   # 传给 load_config()
        cli_home = _read_agent_home_from_file(cli_cfg)   # 泛化自 _read_agent_home_from_project
        if cli_home:
            ... return 相对仓库根解析的 path
    # fallback（不变）：AGENT_HOME env > 项目 yaml.agent_home > ~/.xihe-agent
```

`load_config(config_path)` 则把 `--config` YAML 作为最高优先级 apply（在项目 yaml 之后），并把其**同目录 `.env`** 优先加载。

## .env 跟随与"浮顶"陷阱（最重要的使用注意）

`.env` / 环境变量**整体压在所有 YAML 之上**（12-factor：env > config）。`load_config()` 加载顺序（`override=False`，先到先得 = 高优先）：

```
--config 同目录 .env（sibling，实例凭据，最高）  >  仓库根 .env（项目默认）  >  AGENT_HOME/.env（用户）  >  shell 环境变量
```

**陷阱**：以下 **7 个 env-aliased key** 会被 `.env` 同名变量抢占——

`model`(`MODEL`) / `api_key`(`LLM_API_KEY`…) / `base_url`(`LLM_BASE_URL`…) / `platform`(`PLATFORM`) / `vision_model` / `max_iterations` / `compression_threshold`

→ **实例要让这 7 个 key 各不相同，必须放 sibling `.env`（`configs/.env`），而不能只写 YAML**——否则被仓库根 `.env` 锁死。实测：YAML 写 `model: glm-5-zp` 不生效（被仓库根 `.env` 的 `MODEL=glm-5.2-zp` 压），把 `MODEL=glm-5-zp` 放 `configs/.env` 才生效。

其余键（`platforms` 段 / `models` 目录 / `kbs` / `mcp_servers` / `session` / `auxiliary` / `delegation` / `approvals`）**无 env 别名**，只受 YAML 分层，直接写实例 YAML 即可。

## 数据隔离边界

每个实例的 `agent_home` 独立 → 以下运行时状态天然隔离（无需额外配置）：`sessions.db`（对话历史）/ `agent.log` / `browser/states/*.json`（登录态）/ `cron`（定时任务）/ `cli_history` / memories / todos / ssh / 平台缓存。

13 个常量消费者（`session.py` / `browser_tool.py` / `cronjob_tools.py` / `chat.py` / …）**源码零改动**——它们 import 时 `AGENT_HOME` 已被 peek 算对。

## 验证

- `pytest -q` → 12 全绿（`conftest.isolate_db` monkeypatch `core.session._DB_PATH` 到 tmp，与 `AGENT_HOME` 取值正交、不受影响）。
- smoke：`--config configs/smoke.yaml`（`agent_home: .xihe-smoke`）→ `AGENT_HOME` 重定位 ✓、sibling `.env` 的 `MODEL` 压过仓库根 `.env` ✓、`XIHE_CONFIG_FILE` 导出 ✓；不带 `--config` 时默认 `AGENT_HOME=.xihe-agent` 行为不变 ✓。

## 相关页面

- [[0006_session-design]] — 会话两层 ID；session_key 含 platform/chat_id，多实例靠数据根隔离天然不串
- [[0022_testing-strategy]] — 测试隔离约定 monkeypatch `_DB_PATH`，与多实例 `AGENT_HOME` 解析正交
- [[0001_xihe-agent]] — 项目总览
