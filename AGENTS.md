# Repository Guidelines

## Project Overview

xihe-agent is a single-process, OpenAI-compatible tool-calling agent. One core supports four entry points:

- **CLI**: `xihe chat` runs an interactive REPL or one-shot query in the launch directory.
- **Messaging gateway**: `xihe gateway` exposes WeCom and Feishu chat adapters.
- **Service**: `xihe serve` provides an aiohttp HTTP and WebSocket API for the desktop app and external clients.
- **Desktop**: `desktop/` is an Electron + React + Tailwind control plane that spawns and supervises the service.

All modes share the same `XiheAgent` loop, SQLite sessions, long-term memory, tool registry, skills, approvals, cron scheduler, specialist agents, context compressor, and configuration. The project deliberately keeps the Python core single-process while allowing browser automation, terminal/file operations, MCP tools, media generation/analysis, external agent delegation, and `.biz_kbs` business-knowledge workflows.

The core runs on Python 3.10+ and any OpenAI-compatible chat-completions endpoint. First launch seeds `~/.xihe-agent/config.yaml`; fill in `api_key` and `base_url` there. Never commit populated credentials or generated user data.

## Project Structure

```text
src/core/         Agent loop, config, sessions, compressor, prompts, toolsets, model catalog
src/tools/        Self-registering tool modules and approval interception
src/platforms/    WeCom and Feishu adapters over BasePlatformAdapter
src/gateway/      Messaging bot, HTTP+WS service, stream consumer, slash commands
src/cli/          app.py entry point/SharedContext and chat.py REPL
src/skills/       Bundled skills
desktop/          Electron control plane (separate Node toolchain)
tests/            pytest suite
```

Important implementation files:

- `src/core/agent.py`: `XiheAgent`, tool-call loop, streaming, interrupts, approval requests, and context-budget handling.
- `src/cli/app.py`: argparse CLI and `SharedContext`, which reuses SQLite, auxiliary clients, compressor, scheduler, and loaded registry across gateway messages.
- `src/core/session.py`: deterministic platform/chat session keys, reset policies, and SQLite persistence.
- `src/core/toolsets.py`: roster resolution from `toolsets` and `skills` configuration.
- `src/tools/__init__.py`: registry dispatch, availability checks, result limits, path rewriting, and approval gate.
- `src/gateway/serve.py`: HTTP/WS API and readiness probes.

## Run Modes & Commands

```bash
pip install -e .                  # Editable install with Python dependencies
xihe                              # Interactive chat
xihe chat -s bug-hunt -q ...    # Named one-shot session
xihe chat -r                      # Resume a recent session
xihe gateway --platform wecom     # Run a messaging adapter
xihe serve --host 0.0.0.0 --port 7788
xihe cron list                    # Inspect scheduled jobs
xihe cron create 30m ...        # Add a recurring task
xihe cron run <id>                # Trigger immediately
xihe cron remove <id>             # Delete a job
xihe doctor                       # Config/deps/browser/MCP/connectivity
xihe doctor gateway               # Also check platform credentials
pytest                            # Full Python test suite
```

In `desktop/`:

```bash
npm install
npm run dev       # Development window
npm run build     # Type-check and bundle
npm run preview
```

The desktop spawns `xihe` from `PATH`; set `XIHE_BIN` to override it. Gateway and service processes are long-running, so restart them after code changes.

## HTTP & Streaming API

`xihe serve` defaults to `127.0.0.1:7788`. REST endpoints include health/readiness, model connection testing, agents, sessions, conversation messages/traces, MCP, skills, cron, specialists CRUD, capability-store install/mount, and browser control. `/stream` is the WebSocket chat channel and emits per-turn thinking, text, tool, and approval events. `GET /readiness` and `xihe doctor` expose the same actionable diagnostic set; the API never sends the configured model API key to clients.

## Toolsets

Tools are grouped in `src/core/toolsets.py`:

- `base`: read-only file search/read/tree, skill index/view, memory reads, KBS search, todo, model info, and RestrictedPython sandbox execution.
- `web`: search/extract/crawl plus 40+ browser automation actions, including tabs, frames, cookies, screenshots, login state, and recording.
- `files`, `terminal`, `process`: local writes, patches, command execution, and process management.
- `dev_tool`: code execution, Maven dependency checks, Node version management.
- `http`, `memory`, `communication`, `media`: HTTP requests, memory/session search, outbound messages, vision/OCR/image/TTS.
- `agent`, `external_agents`, `skills`: delegation, Claude/Codex integration, runtime skill management.
- `scheduler`, `ssh`, `kbs`, `meta`, `mcp`: cron, remote execution, business knowledge, runtime capability requests, and MCP tools.

### Roster Rules

An agent surface is `base ∪ roster − blocked`. Every non-empty roster automatically receives the read-only base floor; write and heavy capabilities require explicit inclusion. `enabled_toolsets=[]` means no tools, while `None` means everything—do not collapse them with truthiness. Availability gates hide tools whose optional dependencies or API keys are absent. Subagents lose recursive and user-facing tools.

## Architecture & Invariants

- Gateway messages use lightweight per-message agents backed by `SharedContext`; expensive SQLite, auxiliary client, compressor, scheduler, and registry state is reused.
- Read-only tools run concurrently; any write tool forces sequential dispatch.
- Conversation state persists to SQLite after every iteration.
- Iteration warnings inject at 70% and 90% of `max_iterations`; `interrupt()` can stop the loop from another thread and propagate to children.
- Oversized tool results spill to a side-store instead of entering inline history.
- Context compression uses the configured model context length and threshold.
- MCP discovery, store mounts, and specialist rosters resolve into the same registry/toolset model.

## Configuration

`~/.xihe-agent/config.yaml` is authoritative. `config.example.yaml` documents all fields and should be updated with new settings. Key groups include:

- `model`, `base_url`, `api_key`: main OpenAI-compatible endpoint.
- `toolsets`, `skills`, `models`, `max_iterations`, `compression_threshold`: main-agent capability and context behavior.
- `platforms.wecom`, `platforms.feishu`: gateway credentials and behavior.
- `mcp_servers`: streamable-HTTP or stdio MCP servers.
- `approvals`: mode, timeout, deny/ask/allow rules, LLM judge, and memory retention.
- `specialists`, `external_agents`, `auxiliary`: delegation and auxiliary model configuration.
- `web`, `store.sources`, `kbs.enabled`, `cron`, `agent_home`: optional integrations and instance-local storage.

Use `xihe --config path/instance.yaml ...` for isolated multi-instance homes. Local context files (`AGENTS.md`, `CLAUDE.md`, `xihe.md`, `.cursorrules`) can be injected into prompts according to `session.*` settings.

## Development Guide

### Add a Tool

Both steps are required:

1. In `src/tools/<name>_tool.py`, call `registry.register(...)` at import time with a unique name, OpenAI function schema, handler, availability check, toolset, result-size policy, read-only flag, and path parameters. Handlers accept `(args: dict, **kwargs)` and return JSON strings; context and `parent_agent` arrive as keyword arguments. Do not inject them into `args`.
2. Add the tool to its toolset in `src/core/toolsets.py`. Registered but unlisted tools remain invisible.

Keep dependency declarations synchronized between `pyproject.toml` and `requirements.txt`. Update `config.example.yaml` and README documentation whenever adding configurable behavior.

### Coding Style

- Python targets 3.10+ and follows PEP 8 with four-space indentation.
- Use `snake_case` for modules/functions/variables and `PascalCase` for classes.
- Keep tool schemas and descriptions user-facing and explicit; handlers return controlled JSON errors rather than raising to the agent loop.
- Preserve type hints and docstrings for architectural contracts and optional dependencies.
- Desktop code uses the existing TypeScript, React, Electron, Tailwind, and Vite conventions.

### Approvals & Security

`approvals` gates destructive operations. Deny rules take precedence, listed tools always ask, and allow rules create persistent session exemptions. Manual mode prompts the user through the active surface; unattended delivery denies rather than timing out indefinitely. An optional auxiliary LLM judge can screen terminal commands without bypassing the gate.

When changing terminal, file, browser, SSH, sandbox, execution, or process tools:

- Preserve approval evaluation and avoid alternate paths that bypass the same effect.
- Keep secrets, cookies, browser profiles, generated stores, and `.biz_kbs` content out of Git.
- Treat MCP servers, external agents, specialist agents, and auxiliary models as external trust boundaries.
- Keep redaction enabled for logs by default.

## Testing Guidelines

Tests use pytest and are grouped conceptually as:

- **L0**: pure functions and configuration/model-catalog behavior.
- **L1**: tools with mocked filesystem, process, network, browser, or platform I/O.
- **L2**: agent-loop invariants using fake model clients.

Name files `tests/test_<subject>.py`. Use shared fixtures from `tests/conftest.py` and deterministic doubles from `tests/fakes.py`; avoid live model and network calls. Existing coverage emphasizes approvals, dangerous commands, routing, roster resolution, sessions, stream consumption/attribution, gateway/serve behavior, platform adapters, toolsets, model catalog, skills, diagnostics, and KBS/store behavior.

Run `pytest` before submitting. For focused work, run the directly related test module first, then the complete suite.

## Commits & Pull Requests

- Recent history uses short, lowercase, imperative subjects (for example, `model catalog: layered context-length lookup + live /models discovery`). Keep commits focused and user-visible.
- Pull requests should explain the problem, implementation approach, configuration impact, test evidence, and linked issues.
- Include screenshots for desktop/gateway UI changes, API examples for service changes, annotated samples for new settings, and migration notes for breaking changes.

## License

The project is MPL-2.0. Modified files remain under MPL-2.0; preserve license headers and upstream file-level attribution where present.
