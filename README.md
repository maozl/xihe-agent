# xihe-agent

*Xihé (羲和) — the sun's charioteer in the Chu Ci. The one that drives the sun's journey now drives your tools.*

**A single-process, OpenAI-compatible tool-calling agent that runs from one core in four shapes: an interactive CLI, a messaging gateway, an HTTP+WS service, and an Electron desktop app.**

[English](README.md) | [简体中文](README.zh-CN.md)

Point it at any OpenAI-compatible endpoint (Zhipu, Volcano Ark, DeepSeek, OpenAI, or an internal gateway) and xihe shares one set of tools, skills, memory and configuration across your terminal, WeCom/Feishu, an HTTP service and the desktop app. It doesn't just answer questions — it operates your internal systems with a logged-in browser, runs terminals, reads and writes files, executes scheduled jobs, and shows you every step as it goes.

```
                 ┌──────────────────────────────────────┐
                 │           one agent core             │
                 │   XiheAgent · tool registry ·        │
                 │   sessions · skills · approvals ·    │
                 │   memory · compressor · cron         │
                 └──────┬──────────┬──────────┬─────────┘
                        │          │          │
          ┌─────────────┘          │          └─────────────┐
    xihe chat                 xihe gateway            xihe serve
    interactive CLI           WeCom / Feishu          HTTP + WebSocket
    one-shot queries          chat-message agent      service for the
    named sessions            bots, slash commands,   desktop app and
    session resume            inbound-image OCR       external clients
                                                      ┌──────────────────┐
                                                      │ desktop/ Electron│
                                                      │ control plane    │
                                                      └──────────────────┘
```

## Why xihe

**🧠 One brain, four shapes.** The agent loop, 80+ tools, skills and approvals are shared by `xihe chat`, `xihe gateway`, `xihe serve` and the desktop app — configure once, run everywhere. Long-term memory is shared across all four, so what it knows about you doesn't change with the entrance you use (conversation history stays per-platform).

**🌐 It browses with your login.** xihe drives a dedicated real Chrome over CDP (own profile, own debug port): scan a QR code or pass SSO once, and the login state persists across xihe restarts — from then on it *remembers* your internal systems. Real Chrome carries HSTS memory, so Secure cookies on SSO callbacks don't get silently dropped the way a fresh Playwright context drops them — enterprise single sign-on stops looping.

**👀 Transparent process, yours to interrupt.** Thinking and replies render as separate streams. In WeCom a live feed of thinking gists and tool lines scrolls in real time, then gets replaced wholesale when the reply arrives — fold-like, clean. `/stop` interrupts at any moment; a message sent while a turn is running arrives as a steer at the next iteration boundary — no interruption, just course correction.

**🛡️ Dangerous operations get a human gate.** 39 dangerous-command patterns plus high-risk argument tables and an LLM semantic judge, with one consistent confirmation UX everywhere: CLI prompt, desktop approval card, or chat reply (`y|n|a`). `deny`/`allow` glob rules and a per-session "approve and don't ask again" memory keep the gate from becoming noise.

**🎬 Record once, it knows.** `browser_record` turns a browser session into actions with role/name metadata and a runnable Playwright script; the `web-record-to-skill` skill goes further and distills the recording into a replayable skill. The agent can record its own exploration too (`browser_record_start/stop`) — human and agent actions hit the same recorder.

**🧑‍💼 Specialists with a division of labor.** Drop a YAML in `~/.xihe-agent/agents/` and the main agent gains a `run_<slug>_agent` dispatch tool: its own persona, its own tool and skill whitelist, and optionally its own model connection — routine checks on a cheap model, hard coding on the flagship, one xihe with clear role boundaries. Unlike `delegate_task`'s ad-hoc subagents, specialists run the full layered prompt; the desktop ships a visual editor for them.

**🤝 It can direct other agents.** `external_agent` hands a subtask wholesale to an external CLI agent (claude), reusing xihe's model credentials (auto-converted to the matching environment variables) — no separate credential set to maintain. xihe orchestrates; the external agent plays support.

**📚 It learns your business over time.** The `.biz_kbs` protocol is a living knowledge base organized by business domain — append-only raw sources for traceability, layered wiki / staging candidates, a controlled vocabulary for ownership. It writes only when you explicitly say "record this"; ordinary Q&A never touches the store. In any later session, `kbs_search` brings back the conclusions from earlier research.

**🧩 Skills load on demand, MCP hot-swaps.** The prompt carries only the skill index; bodies enter context via `skill_view` when actually used. MCP servers hot-reload with `/reload-mcp` (no restart) and mount per agent as different subsets; mid-turn, the agent can request the `web` / `media` / `scheduler` toolsets on the spot via `request_tools` — lean by default, present when needed.

**🔌 Any OpenAI-compatible model.** Zhipu, Volcano Ark, DeepSeek, OpenAI or an internal gateway — switching is a `base_url` change. `/model` auto-discovers the models your endpoint offers; context lengths for common families resolve from a built-in catalog, so compression thresholds need no hand-tuning.

And more: 17 toolsets, runtime skill creation, task delegation, SQLite sessions with crash recovery, long-term memory, cron jobs, a capability store, side-by-side instances — see below.

## 60-second start

```bash
git clone <repo-url> && cd xihe-agent
pip install -e .            # or: pip install -r requirements.txt
xihe                        # first start seeds ~/.xihe-agent/config.yaml (fully annotated)
```

Open `~/.xihe-agent/config.yaml` — fill in `api_key`, adjust the model connection and the toolsets that decide what the agent can do:

```yaml
model: glm-4.6              # any OpenAI-compatible model
base_url: https://open.bigmodel.cn/api/paas/v4/
api_key: sk-...
toolsets: ["files", "terminal", "web", "http", "memory", "mcp", "kbs"]
skills: ["*"]                # inject the full skill index; [] = none
```

Talk to it:

```bash
xihe                          # interactive chat (default subcommand)
xihe chat -q "summarize ."   # one-shot, non-interactive
xihe chat -s my-project      # named session
xihe chat -r                  # list & resume a recent session
```

On first launch nothing else is required: if `config.yaml` doesn't exist, xihe seeds it from the annotated template and tells you to fill in `api_key` there; the desktop app shows a welcome card that walks you to Settings.

## Requirements

- Python ≥ 3.10
- An OpenAI-compatible chat-completions endpoint (model name, `base_url`, `api_key`)
- Optional, per feature: Playwright (browser tools), PaddleOCR/PaddlePaddle (offline `image_ocr`), search API keys (`web_search`)

## Configuration

`~/.xihe-agent/config.yaml` is the single source of truth — see [`config.example.yaml`](config.example.yaml) for the fully annotated reference. Highlights:

| Key | Meaning |
| --- | --- |
| `model` / `base_url` / `api_key` | Main agent connection (OpenAI-compatible) |
| `toolsets` / `skills` | Main agent roster: `[]` = no tools, `["*"]` = unrestricted, names = whitelist |
| `models` | Model catalog: register `context_length` per model (beats the built-in table); the `/model` list = this section ∪ endpoint discovery |
| `vision_model` | Multimodal model for `vision_analyze` (main model may be text-only) |
| `max_iterations` / `compression_threshold` | Agent-loop budget and context-compression trigger |
| `specialists.enabled` | Master switch for specialist delegation (default off) |
| `platforms.wecom` / `platforms.feishu` | Gateway adapter credentials |
| `mcp_servers` | Named MCP servers (`streamable-http` / `stdio`) |
| `approvals` | Dangerous-operation gate: `mode`, `timeout`, `deny`/`allow` rules, `llm_judge` |
| `auxiliary` | Separate models for vision / image-gen / TTS / approval judge |
| `web` | Search/scrape API keys (tavily, serpapi, bing, firecrawl) — empty key = tool hidden |
| `store.sources` | Capability-store index URLs (HTTP or local path) |
| `kbs.enabled` | Business knowledge base (`.biz_kbs`) tools |
| `agent_home` | Instance data root (only meaningful in a `--config` instance file) |

## Run modes

### CLI — `xihe chat`

```bash
xihe                          # interactive REPL in the current directory
xihe chat -s bug-hunt -q "find flaky tests and propose fixes"
xihe chat -r                  # resume a previous session
```

The agent's working directory is where you launched it; `CLAUDE.md` / `xihe.md` / `AGENTS.md` / `.cursorrules` in that directory are injected into the system prompt when present (toggle via `session.*`).

### Messaging gateway — `xihe gateway`

Turns chat messages into agent turns on WeCom (WebSocket) or Feishu:

```bash
xihe gateway                       # platform from config
xihe gateway --platform wecom
```

Inbound images are auto-described via vision/OCR before reaching a text-only main model. Messages starting with `/` are slash commands handled before the agent. A plain message sent while a turn is running arrives as a steer (effective at the next iteration boundary); `/stop` or a plain "stop" interrupts immediately. Gateway is a long-running process — **restart it to pick up code changes**.

### HTTP + WebSocket service — `xihe serve`

The same agent core fronted by aiohttp, for the desktop app or any external client:

```bash
xihe serve                         # 127.0.0.1:7788
xihe serve --host 0.0.0.0 --port 7788
```

REST surface includes `/health`, `/readiness` (structured what's-missing report), `/test-connection` (server-side model probe — the api_key never crosses the API), `/agents`, `/sessions`, `/convs/{id}/messages`, `/convs/{id}/trace/{msg}`, `/mcp`, `/skills`, `/cron`, `/specialists` (CRUD), `/store` (install/uninstall/mount), and `/browser/*` (embedded-browser control); `/stream` is the WebSocket chat channel with per-turn thinking/text/tool/approval events.

### Health check — `xihe doctor`

One command, actionable checklist — every failing line names the fix (config field or install command):

```bash
xihe doctor            # config, deps, browser, capability matrix, MCP, connectivity
xihe doctor gateway    # also checks platform credentials
```

The same probe set backs `GET /readiness`.

### Desktop — `desktop/`

An Electron + React + Tailwind control plane (own Node toolchain, no code shared with the Python core). It spawns the `xihe` CLI from `PATH` (`XIHE_BIN` to override), supervises the serve process, and edits `~/.xihe-agent/config.yaml` over IPC:

```bash
cd desktop
npm install
npm run dev      # dev window
npm run build    # type-check + bundle
```

The desktop ships workspaces (bind conversations to project directories), an embedded browser panel (Chrome snapped into the window, light/dark follows the app), the capability store (browse/install/mount skills and MCP), a specialist-agent editor, and a settings page.

> Air-gapped / restricted networks: configure your npm registry and `electron_mirror` globally in `~/.npmrc` — the Electron binary postinstall otherwise downloads from github.com and fails offline.

## Tools & toolsets

Every tool self-registers at import time; `src/core/toolsets.py` groups them:

| Toolset | Contents |
| --- | --- |
| `base` *(auto-included)* | `read_file`, `search_files`, `directory_tree`, `skills_list`, `skill_view`, memory reads, `kbs_search`, `todo`, `model_info`, `run_sandbox_code` (RestrictedPython) |
| `web` | `web_search` / `web_extract` / `web_crawl` + 40+ `browser_*` automation tools (navigate, click, type, tabs, frames, cookies, screenshots, login-state save/load, action recording) |
| `files` | `write_file`, `patch` |
| `terminal` | `terminal`, `process` |
| `dev_tool` | `execute_code`, `maven_dep`, `node_version` |
| `http` | `http`, `request_tools` |
| `memory` | `memory_manage`, `session_search` |
| `communication` | `send_message`, `send_image`, `clarify` |
| `media` | `vision_analyze`, `image_ocr`, `image_generate`, `text_to_speech` |
| `agent` | `delegate_task` |
| `external_agents` | `external_agent` (claude CLI) |
| `skills` | `skill_manage` |
| `scheduler` | `cronjob` |
| `ssh` | `ssh_connect`, `ssh_exec`, `ssh_disconnect`, `ssh_status` |
| `kbs` | `kbs_init` (search/status live in `base`) |
| `meta` | `request_tools` (ask for `web`/`media`/`scheduler` at runtime) |
| `mcp` / `mcp-<server>` | all / one MCP server's tools |

An agent's surface = **base ∪ roster − blocked**: every non-empty roster auto-includes the read-only `base` floor (file reads, skill index, memory reads, an in-process compute sandbox); write and heavy capabilities are granted per roster; recursion/user-face tools are stripped from all subagents. `check_fn` acts as an availability gate: browser tools vanish entirely when Playwright isn't importable; search tools vanish without an API key.

## Dangerous-operation approvals

`approvals` gates destructive actions behind a three-valued decision pipeline (`allow / ask / deny`):

```yaml
approvals:
  mode: manual            # manual = ask for dangerous ops | auto = allow all
  timeout: 300            # seconds to wait for an answer
  timeout_action: deny    # what to do on timeout
  llm_judge: true         # auxiliary-LLM semantic check for regex misses
  deny:                   # hard gates, never prompted
    - "terminal(*mkfs*)"
    - "ssh_exec"
  allow:                  # persistent whitelist
    - "terminal(rm -rf /tmp/*)"
```

Rule syntax is `"tool(glob)"` — for `terminal`/`ssh_exec` the glob matches the raw command; for other tools it matches `action` + key arguments. Decision order: `mode: auto` > `deny` rules > `allow` rules > session memory > danger heuristics. "Approve and don't ask again" (`a` in chat, the third desktop button) silences the same danger class for the session only; cross-session relief goes through `allow`. Unattended cron runs with no confirmation channel are denied — set `mode: auto` if a job must run dangerous commands.

The heuristics (39 command patterns + high-risk argument tables + LLM judge) are a convenience gate, not a security boundary.

## Skills

A skill is a directory with a `SKILL.md` (YAML frontmatter: `name`, `description`) plus optional `scripts/` and reference files. Bundled skills live in `src/skills/`; user skills in `~/.xihe-agent/skills/`. The agent lists/views them via `skills_list` / `skill_view`, creates and edits them via `skill_manage`, and the `web-record-to-skill` skill can record a browser session into a replayable skill.

## Specialist agents

One YAML file per specialist in `~/.xihe-agent/agents/` (filename = slug), gated by `specialists.enabled`:

```yaml
persona: "You are a release engineer..."
toolsets: ["terminal", "dev_tool"]
skills: []
# model / base_url / api_key / max_iterations — unset keys inherit the main config
```

With the gate on, each file registers a `run_<slug>_agent(goal, context)` tool and the main agent's prompt gains a roster layer routing work to it. Unlike `delegate_task`'s bare task-card subagents, specialists run the full layered prompt with their own persona and whitelist — and can even point at a different model endpoint.

## Sessions, memory, cron

- **Sessions** are SQLite rows keyed deterministically from platform + chat + user (`agent:main:cli:dm:...`). History is persisted after every loop iteration; on load, dangling tool calls from a crashed turn are repaired automatically. Model choice can be overridden per session.
- **Memory** is long-term, namespaced (main agent vs each specialist), and injected as a snapshot into each turn.
- **Cron** jobs persist under `~/.xihe-agent/cron/` and run unrestricted agents:

```bash
xihe cron list
xihe cron create 30m "check the build dashboard and report failures" --name build-watch
xihe cron run <job_id>
xihe cron remove <job_id>
```

## Multi-instance

```bash
xihe --config ~/instances/support.yaml gateway
```

The instance file is the single config source for that process; its optional `agent_home` isolates the data root (`sessions.db`, `agent.log`, `browser/`, `cron/`, skills, `.biz_kbs`). Run gateway, serve, and CLI instances side by side without cross-talk.

## Architecture

```
src/
├── core/          XiheAgent loop, config, sessions, compressor, prompts, toolsets, model catalog
├── tools/         tool modules — each self-registers into the registry at import
├── platforms/     WeCom (WebSocket) & Feishu adapters over BasePlatformAdapter
├── gateway/       bot.py (messaging gateway) · serve.py (HTTP+WS service) · stream consumer · slash commands
├── cli/           chat.py REPL + app.py entry point / SharedContext
└── skills/        bundled skills
desktop/           Electron control plane (separate Node toolchain)
tests/             pytest suite — L0 pure functions, L1 tools w/ mocked IO,
                  L2 agent-loop invariants via a fake model client
```

Agent-loop invariants worth knowing before contributing: read-only tool calls run concurrently, any write tool forces sequential dispatch; messages persist to SQLite after every iteration; a nudge/warning injects at 70%/90% of `max_iterations`; oversized tool results spill to a side-store instead of inline history; `agent.interrupt()` stops the loop from another thread and propagates to children.

## Development

**Add a tool — both steps are required:**

1. Register it in a `src/tools/*.py` module: `registry.register(name, schema, handler, check_fn=..., toolset="...", read_only=...)` at import time. Handlers take `(args: dict, **kw)` (context and `parent_agent` arrive as kwargs) and return a JSON string.
2. List it in a toolset in `src/core/toolsets.py`. Registered-but-unlisted tools are invisible to agents.

**Gotchas:**

- Gateway and serve are long-running — restart to pick up code changes.
- `enabled_toolsets=[]` means "no tools"; `None` means "everything". Don't collapse them with a truthiness check.
- Browser tools vanish entirely if Playwright isn't importable (`check_fn` gate).
- Keep `requirements.txt` and `pyproject.toml` dependency lists consistent.

**Test:**

```bash
pytest
```

## License

[MPL-2.0](LICENSE) — file-level copyleft: free to use, modify, and distribute, including as part of a larger proprietary work; only files you modify must remain open under MPL-2.0.
