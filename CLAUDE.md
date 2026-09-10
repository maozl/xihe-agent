# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`xihe-agent` is a single-process, OpenAI-compatible **tool-calling agent** that runs in two modes from one core: an interactive CLI (`xihe chat`) and a messaging **gateway** (WeCom WebSocket, Feishu) that turns chat messages into agent turns. Python ≥3.10. The agent loop, tools, sessions, and skills are shared across both modes.

### `desktop/` — Electron control plane (separate Node toolchain)

The repo also ships a desktop app under `desktop/` (electron-vite + React + Tailwind). It is a **self-contained sub-project with its own toolchain** — no code is shared with the Python core; it consumes xihe over the `xihe serve` HTTP/WS protocol. It spawns the `xihe` CLI found on `PATH` (`XIHE_BIN` to override), supervises the process, and edits `~/.xihe-agent/config.yaml` over IPC (single source). Run it from the repo root:

```bash
cd desktop && npm install && npm run dev    
```

## Commands

Install (editable) and run via the `xihe` entry point (`pyproject.toml` → `xihe = app.main:main`):

```bash
pip install -e .            # or: pip install -r requirements.txt
xihe                        # interactive chat (default subcommand)
xihe chat -q "hello"        # one-shot query, non-interactive
xihe chat -s my-session     # named session
xihe gateway                # run messaging gateway (platform from config, default wecom)
xihe gateway --platform wecom
xihe --config ~/xihe-instance.yaml gateway   # instance config: own data root + overrides (run multiple xihes)
xihe cron list              # scheduled jobs (create | remove | run)
xihe --version
```

There is a small **pytest** suite under `tests/` — L0 pure functions, L1 tool handlers with mocked IO, L2 agent-loop invariants driven by an injected fake model client (`FakeChatClient`). Run it with `pytest`. The layered strategy is documented in `.project-kbs/wiki/concepts/0022_testing-strategy.md`. For quick syntax checks use `python -m py_compile <file>`; for runtime behavior, run the relevant mode (`xihe chat` / `xihe gateway`).

Runtime state lives under `~/.xihe-agent/` (override with `AGENT_HOME` or `--config`):
- `config.yaml` — **single source** of all config and credentials (see Config below)
- `agent.log` — log file (gateway logs here; check it when debugging)
- `sessions.db` — SQLite conversation history
- `browser/states/*.json` — saved browser login states; `skills/` — user-created skills
- `agents/*.yaml` — specialist-agent definitions (one file per specialist; see Specialist agents below)

Config/credentials: everything lives in `~/.xihe-agent/config.yaml` — model, `api_key`, `base_url`, `platforms`, `mcp_servers`, etc. There is **no `.env` and no environment-variable override** (no `LLM_*` / `OPENAI_*` / `MODEL` reads); the YAML is the single source of truth and its values are literal. `config.example.yaml` (repo root) is a reference template — copy it to `~/.xihe-agent/config.yaml` and edit.

**The gateway is a long-running process — restart it to pick up code changes** (tool schemas, prompts, and module-global browser state are cached for the process lifetime).

## Architecture

### Tool registry + toolsets (central pattern)
- Each `src/tools/*.py` calls `registry.register(name, schema, handler, check_fn=..., toolset="...", read_only=...)` **at import time**. The registry contract — `ToolRegistry`, the `registry` singleton, `tool_result()`/`tool_error()` (JSON-string return convention), and dispatch (approval gate, path rewriting) — lives in `src/core/registry.py`; `src/tools/__init__.py` re-exports it and owns `load_all_tools()` (static imports only, idempotent), `start_mcp_discovery()` (background MCP connect), and `wait_for_mcp()` — process-level orchestration (static load → MCP discovery → specialist registration) lives in `SharedContext.__init__`, the composition root every mode constructs. **Core never imports tools at module level** (importing a tool module registers it — an import side effect); function-level delayed imports are the accepted seam, except from `core/support/` — zero-dependency machinery (approvals, interrupt, redact, tool-result side-store, path rewriting, process/env utils, the cross-process `CrossProcessLock`) importable at module level from anywhere.
- Handlers receive `(args: dict, **kw)` and return a JSON **string**. `context` (chat_id, platform, session_key) and `parent_agent` flow through as kwargs, not inside `args`.
- **`src/core/toolsets.py`** defines toolset groups (`base`, `files`, `terminal`, `dev_tool`, `http`, `web`, `media`, `agent`, `skills`, …) — flat name→tools lookups with Chinese `label`s. `XiheAgent` calls `registry.get_schemas(toolsets=self.enabled_toolsets)`; `enabled_toolsets=None` exposes every tool whose `check_fn` passes, `[]` exposes **the base floor only** (config-driven rosters rely on this distinction). `mcp-<server>` names are per-server MCP scopes matched via registry membership, not TOOLSETS entries.
- **Three-layer tool model** — an agent's surface = (base ∪ roster) − blocked: the **base** toolset (side-effect-free faces: file reads, skills index/view, memory get/list/search, kbs_search/status, todo, model_info, and the in-memory compute sandbox `run_sandbox_code` — RestrictedPython, gated on importability) is auto-unioned into every restricted roster in `XiheAgent.__init__` — `[]` included (a restricted agent always keeps the read-only floor; "pure-chat zero tools" was dropped as a footgun); only `None` (unrestricted) doesn't need the union. **roster** grants write/heavy/domain faces — read/write bundles split along the base line (`files` roster = write only, `memory` = save + session_search only, `skills` = manage only, `kbs` = init only). **blocked** (`subagent_blocked=True` at registration; documented manifest `SUBAGENT_BLOCKED_TOOLS`, asserted against the registry by tests) strips recursion / user-face / escalation tools from every subagent regardless of roster.
- **Config-driven roster (main + specialists share one semantics):** the main agent's tools/skills are **config.yaml top-level keys** (`toolsets` / `skills`, next to `model`); specialists carry the same key names in `agents/<slug>.yaml`. **Absent/empty → base floor only (XiheAgent force-unions base into every restricted roster); `["*"]` → unrestricted; `mcp` = all servers, `mcp-<server>` = one.** Both go through one function, `core.toolsets.resolve_roster(spec)`. `SharedContext` stores `main_toolsets`/`main_skills` once; CLI, gateway, and serve all pass them to `create_agent`. `create_agent()` with no args (cron jobs, slash commands) stays unrestricted. Delegate children are scoped **independently of the parent roster** (a slim main would otherwise starve them): requested toolsets are honored as-is, no request → `delegate_tool.DELEGATE_DEFAULT_TOOLSETS`, `["*"]` → unrestricted; per-tool safety comes from `subagent_blocked` tags, not roster inheritance.
- **To add a tool you must do BOTH:** register it in a `src/tools/` module **and** list it in a toolset in `src/core/toolsets.py`. A registered-but-unlisted tool is invisible to the agent.
- `check_fn` is an **availability gate**: if it returns falsy, the tool is silently dropped from the schema. Browser tools gate on Playwright being importable — if Playwright isn't installed, *all* `browser_*` tools disappear (this looks like "the agent has no browser tools").

### Agent loop (`src/core/agent/agent.py`, `XiheAgent.chat`)
Standard OpenAI chat-completions tool-calling loop, with several invariants worth knowing before editing:
- **Parallel vs sequential dispatch (order-preserving segmentation):** consecutive `read_only=True` calls in a batch coalesce into a parallel group (`ThreadPoolExecutor`); every non-`read_only` call runs alone, in model order — a read *after* a write still observes the write. One write no longer serializes the batch's independent reads.
- **Persistence:** messages are rewritten to SQLite after every iteration, so an interrupt/crash mid-turn loses no work.
- **Crash recovery:** on load, `_repair_dangling_tool_calls` appends error results for unanswered tool_calls (otherwise the API rejects the history) and `_inject_recovery_hint` resumes an interrupted task. Don't strip these.
- **Budget pressure:** at 70%/90% of `max_iterations`, a nudge/warning is injected into the last tool result to force consolidation.
- **Compression:** `ContextCompressor` summarizes history when it exceeds `compression_threshold` of the model's context length, then rebuilds the system prompt.
- **Interruptible:** `agent.interrupt()` (from another thread) stops the loop and propagates to child/delegate agents.
- Tool results over the per-tool size limit are spilled to a side-store via `src/core/support/tool_result_storage.py` (look for `maybe_persist_tool_result` / `enforce_turn_budget`) rather than inlined into history.

### Specialist agents (bundled `src/agents/*.yaml` + user `<agent_home>/agents/*.yaml` → derived `run_<slug>_agent` tools)
One YAML file per specialist (file name = slug; validated in `src/core/services/agent_defs.py` — invalid files warn and skip, never crash startup). Two provenance tiers, same loader and format: **bundled** agents (`src/agents/`, currently `explore`, `xihe_guide` (both read-only via explicit `["base"]` rosters) and `coder` (write faces `files`/`terminal`/`dev_tool`, `project_context`, sticky)) are harness features and register **regardless of `specialists.enabled`** — same split as bundled vs user skills; **user** agents are gated by config `specialists.enabled` (default off — ordinary users don't need self-defined specialist delegation). When the gate is off, no user `run_*_agent` tool registers and the roster layer lists only bundled agents (the layer filters by actually-callable tools). **Bundled agents win their slugs** (`_merged_specs`): a user file for a bundled slug is a **delta** — only the override keys `enabled`/`model`/`base_url`/`api_key`/`max_iterations` merge onto the bundled base (so the product can update its content freely, and an instance can e.g. point coder at a cheaper model); any other key in it is ignored with a warning, and `PUT /specialists/{slug}` rejects such writes outright (`bundled_write_violation`). Disable = a one-line `enabled: false` delta; delete the delta to restore the builtin. Each effective file auto-registers a `run_<slug>_agent(goal, context)` tool in the `agent` toolset at `load_all_tools()` time (exposure still requires the `agent` toolset in the main roster); the main agent's prompt gains a roster layer listing them; `GET /specialists` reports `specialists_enabled` so clients can tell "off by config" from "needs restart". Unlike `delegate_task`'s ad-hoc subagents (wholesale prompt override, no guidance layers), a specialist agent runs the **full layered prompt** with its `persona` as the identity layer, its configured `toolsets`/`skills` whitelist, and optional `project_context`. A specialist may override the main agent's connection keys (`model`/`base_url`/`api_key`/`max_iterations`); unset keys inherit the main config at dispatch time (`AgentDef.config_overrides()` overlaid in `specialist_agent_tool._build_agent_instance`). By default every dispatch opens a fresh timestamped session (clean-room, "knows nothing"); `sticky_session: true` re-enters one session per (parent conversation, slug) instead — chat_id `{parent_session_key}:{slug}` via `_approval_shared["session_key"]` — so a sticky specialist remembers its own previous dispatches within the same conversation (never across conversations; the run_* tool description tells the model this). `api_key` never crosses the serve API — `GET /specialists` returns only `api_key_set`, and `PUT /specialists/{slug}` keeps the stored key when the body omits it. **Edits go live without a restart in serve**: every `PUT`/`DELETE /specialists` calls `sync_specialist_agent_tools()` (`specialist_agent_tool.py`), which reconciles the live registry with the yaml state (register/replace/deregister; the `_specialist_tools` ownership set keeps a same-named static tool from ever being deregistered) — the next message's fresh agent picks up the new schemas and roster layer. In-flight dispatches keep the def their handler closure was built with. The `specialists.enabled` gate still follows the config save+restart flow. Gateway mode (no serve API) still restarts to pick up hand edits.

### Two run modes, one agent core
- **CLI** (`src/cli/chat.py`): one long-lived `XiheAgent`.
- **Gateway** (`src/gateway/bot.py`): a `SharedContext` (`src/core/context.py`) owns expensive objects (SQLite conn, `AuxiliaryClient`, `ContextCompressor`) and constructs a **fresh `XiheAgent` per inbound message** (this is cheap). **SharedContext construction is side-effect-free** — process-level orchestration lives in `bootstrap_process(ctx, adapter=None)` (`core/context.py`), called explicitly once by every entrypoint (bot / serve server / CLI `init_agent`): static tool load → MCP discovery → specialist registration → cron wiring (agent factory + scheduler; the scheduler's cross-process file lock in `core/services/scheduler.py` keeps a gateway and a serve on one agent_home from double-firing jobs). Platform adapters in `src/gateway/platforms/` (`WeComAdapter` over WebSocket, Feishu) implement `BasePlatformAdapter`. Messages starting with `/` are slash commands (`src/gateway/commands.py`), handled before the agent. Inbound images are auto-described via vision/OCR before reaching the model.

### Sessions (`src/core/session.py`, `SessionDB`)
SQLite-backed. `SessionSource` (platform + chat_id + user) → deterministic key like `agent:main:{platform}:dm:{chat_id}`. History and the cached system prompt are stored per session; model can be overridden per session. The session key is also the cron-job/cancellation unit.

### Config (`src/core/config.py`)
**Single source.** All config — top-level keys (`model`, `base_url`, `api_key`, `platform`, `max_iterations`, `compression_threshold`, `vision_model`, `toolsets`, `skills`, `language`) and sections (`models`, `platforms`, `mcp_servers`, `session`, `auxiliary`, `delegation`, `external_agents`, `approvals`, `kbs`, `specialists`) — is read from one config.yaml. Values are literal: there is **no `.env`, no environment-variable override, and no `${VAR}` expansion**. Default location: `~/.xihe-agent/config.yaml`. Note `load_config` copies sections from an allowlist — new sections must be added to both loops in `src/core/config.py` or they silently vanish.

`xihe --config path/to/x.yaml` selects an **instance**: that file is the single config source, and its optional `agent_home` overrides the data root (`AGENT_HOME`, so `sessions.db` / `agent.log` / `browser` / `cron` isolate per instance). `AGENT_HOME` is resolved at import time by peeking `sys.argv` (`--config`) and the `AGENT_HOME` env var, so it's correct before any consumer module pins it; the chosen config path is stashed in `XIHE_CONFIG_FILE` so dynamic `load_config()` callers (which pass no path) still see the right instance.

### Auxiliary LLM client (`src/core/agent/auxiliary_client.py`)
Distinct from the main agent model — used for tool-internal LLM calls (vision analysis, context compression, title generation). Vision/image tools route here, not through the main model.

### Skills
A skill is a directory `SKILL.md` (YAML frontmatter: `name`, `description`, optional `version`/`metadata`) plus optional `scripts/` and reference files. **Bundled** skills ship in the repo `src/skills/`; **user** skills live in `~/.xihe-agent/skills/`. Managed at runtime via `src/tools/skill_manager_tool.py` and exposed to the agent via `src/tools/skills_tool.py` (`skills_list`, `skill_view`, `skill_manage`).

### Browser tools (`src/tools/browser_tool.py`, Playwright)
All `browser_*` tools share **module-global** state (`_page`, `_context`, `_browser_instance`) that persists across per-message agents in the gateway process. Launch prefers **system Chrome/Edge channels** over bundled Chromium (bundled Chromium can't reach internal network resources here). Login persistence uses `browser_state_save`/`browser_state_load` (cookies+localStorage as JSON).
- **Playwright sync-API rule:** never call `page.*` methods from inside event-handler callbacks (e.g. `framenavigated`) — it deadlocks the greenlet dispatcher. Do capture work inside `add_init_script` JS instead, and guard any `sessionStorage`/storage access (it throws `SecurityError` on `about:blank`/sandboxed frames and can break popup navigation).

## Environment constraints (this deployment)
- **Airgapped internal network.** Packages install from an internal PyPI mirror; there is no public internet for browser/CDN downloads. Use system Chrome/Edge for Playwright (don't rely on `playwright install chromium`).
- **`requirements.txt` is the source of truth for dependencies** — keep it in sync when adding deps (it lists more than `pyproject.toml`, e.g. `playwright`, `paddleocr`).
- **Default model `glm-5.2-zp` (Zhipu, via an OpenAI-compatible internal gateway) is not multimodal.** Vision/image tasks must go through `vision_model` (set in config) or `image_ocr` (PaddleOCR/PaddlePaddle, run offline) — never assume the main model can see images.

## Gotchas
- Adding a tool = register **and** add to a `src/core/toolsets.py` toolset. Missing either and the agent won't use it.
- Browser tools vanish entirely if Playwright isn't importable (check_fn gate) — symptom: "agent has no browser tools."
- Gateway changes need a process restart to take effect.
- `enabled_toolsets=[]` means "base floor only", `None` means "everything" — don't collapse them with a truthiness check (agent.py relies on `is not None`).
- Core must not import tools at module level (tool import = registration side effect). The registry contract is `core/registry.py` — import it directly instead of reaching into tools.
- Tool modules hold only tool faces (schemas + thin handlers); subsystems (jobs store, loops, locks, execution) live in `core/services/` — e.g. the cron scheduler in `core/services/scheduler.py`, with `tools/cronjob_tools.py` as its tool surface plus re-exports for legacy import paths. And process-level orchestration is explicit: `bootstrap_process()`, never hidden in a constructor.
- Cron delivery: outbound senders are **channels** (`register_channel` in `core/services/scheduler.py`; contract `async send(target, message) -> bool`), routed by target platform name with the gateway adapter as legacy fallback (adapter success = "didn't raise", channels return bool). The `*Adapter` suffix is reserved for `BasePlatformAdapter` subclasses. serve registers `DesktopChannel` under both `desktop` and `serve`, so `deliver: origin` on desktop-created jobs pushes a `cron_result` WS event to the creating conversation; `deliver` accepts comma-separated or list multi-targets (partial failures logged, never fail the run). All cron files live under `AGENT_HOME/cron/` (`jobs.json`, `output/`, `scripts/`, `scheduler.lock`).
- Keep `requirements.txt` and `pyproject.toml` dep lists consistent.

## Code comments

Comments transfer context the reader cannot get from the code itself. **Default to none**; add one only when the code alone cannot convey: **why** a decision was made, what **invariant** must hold, a **trap** that isn't visible locally, or links to issues / RFCs / design notes. Keep each comment as short as the context allows — if removing it loses no context, it shouldn't exist.

Forbidden (delete on sight, in existing code and in review): paraphrasing adjacent code · narrating removed/changed code ("used to…", "previously", "unchanged from before") · restating a condition the code already shows · cross-function line references · explaining another function's behavior · defensive notes aimed at reviewers · section-label banners (`# ---- Title ----`). Module / class / function docstrings follow the same rule — a one-line purpose statement is fine; a paragraph that paraphrases the body is not.

## 项目知识库

本项目使用 `.project-kbs/` 维护项目知识（架构决策、设计规范、踩坑经验）。**会话开始时请先阅读**：

- `.project-kbs/wiki/active.md` — 当前活跃工作
- `.project-kbs/wiki/recent.md` — 最近更新
- `.project-kbs/meta/lint-status.json` — 知识库健康度

完整工作流和页面模板见 `.project-kbs/PROJECT_WIKI.md`。

## 关键约定

- 用户表达"收录/记下来/正式记/先存着/整理"等意图时，按 PROTOCOL.md 的工作流操作 `.project-kbs/`
- 用户未表达记录意图时，只建议不写入
- 高影响操作（删除页面、批量清理、目录重构）必须先确认
- 候选笔记（`meta/candidates/`）不是正式结论，不作为唯一依据