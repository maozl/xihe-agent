"""Core agent loop — OpenAI-compatible tool-calling agent."""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import httpx
from openai import OpenAI

from core.auxiliary_client import AuxiliaryClient
from core.compressor import ContextCompressor
from core.config import load_config
from core import model_catalog
from core.session import SessionDB, SessionSource
from tools import registry, load_all_tools, tool_error

logger = logging.getLogger(__name__)


_PRINTED_SYSTEM_PROMPT_ONCE = False

# Internal empty-response recovery prompts. The "[系统提示]" prefix marks them
# as agent-internal so the serve history fold can keep them out of display
# (they stay in the model-facing history the model needs to see).
_EMPTY_NUDGE = "[系统提示] 上一轮输出为空。请继续当前任务并给出实质回应。"
_EMPTY_RESPONSE_WARNING = (
    "⚠️ 模型连续返回空响应（上下文可能过长），任务未完成。"
    "可发送任意消息让我继续，或考虑重置会话。"
)


def _strip_budget_warnings(messages: list[dict]):
    """Remove stale budget warnings from tool results in history.

    Budget warnings are turn-scoped signals. If left in replayed history,
    models interpret them as still-active instructions and avoid making
    tool calls in ALL subsequent turns.
    """
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "_budget_warning" in parsed:
                del parsed["_budget_warning"]
                msg["content"] = json.dumps(parsed, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass


class XiheAgent:
    """Minimal tool-calling agent backed by OpenAI-compatible API."""

    def __init__(self, config: dict = None, *,
                 enabled_toolsets: list[str] | None = None,
                 delegate_depth: int = 0,
                 is_subagent: bool = False,
                 system_prompt_override: str | None = None,
                 identity_override: str | None = None,
                 skills_allowed: set = None,
                 project_context: bool = True,
                 shared_db: 'SessionDB' = None,
                 shared_aux: 'AuxiliaryClient' = None,
                 shared_compressor: 'ContextCompressor' = None,
                 cwd: str | None = None,
                 client=None):
        # Deferred from module import time: loading registers tools AND
        # connects MCP servers — that must not fire while the caller is
        # still checking the api_key gate (bad mcp_servers used to spew
        # connection errors before any config guidance could print).
        load_all_tools()
        self.config = config or load_config()
        # Injectable for tests (FakeChatClient); defaults to a real OpenAI client.
        self.client = client or OpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["base_url"],
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        self.model = self.config["model"]
        self.max_iterations = self.config["max_iterations"]
        self.max_completion_tokens = int(self.config.get("max_completion_tokens") or 8192)
        # Extra request fields (reasoning_effort, thinking, ...) forwarded as
        # extra_body so unsupported gateway params can be A/B-tested from
        # config without code changes; absent = nothing forwarded.
        self.request_extra = dict(self.config.get("request_extra") or {})

        # Shared state (reused across per-message instances in gateway mode)
        self.db = shared_db or SessionDB(config=self.config)

        # Sub-agent isolation params. Empty list ≠ None: [] = configured to
        # load nothing (config-driven rosters), None = unrestricted.
        self.enabled_toolsets = set(enabled_toolsets) if enabled_toolsets is not None else None
        # Base floor for every agent with a non-empty roster (read faces +
        # autonomy). Falsy check deliberately leaves [] (pure-chat contract)
        # and None (already unrestricted) untouched.
        if self.enabled_toolsets:
            self.enabled_toolsets.add("base")
        self.delegate_depth = delegate_depth
        self.is_subagent = is_subagent
        self.system_prompt_override = system_prompt_override
        # Replaces only the identity layer of the layered prompt (persona
        # agents); system_prompt_override replaces the whole prompt.
        self.identity_override = identity_override
        # Empty set ≠ None: None = no restriction (full skill index, the main
        # agent's default); empty set = no skills at all (a specialist that
        # selected none).
        self.skills_allowed = set(skills_allowed) if skills_allowed is not None else None
        self.project_context = project_context

        # CLI passes the launch dir; gateway passes None so no cwd is injected.
        self.cwd = Path(cwd).resolve() if cwd else None

        # Auxiliary client for tool-internal LLM calls (vision, compression, etc.)
        self.aux = shared_aux or AuxiliaryClient(
            base_url=self.config["base_url"],
            api_key=self.config["api_key"],
            model=self.model,
            config=self.config,
        )

        self._session_models: dict[str, str] = {}
        self._lock = threading.Lock()

        # Mid-turn toolset expansion (request_tools writes here; loop re-reads)
        self._expansion_state: set[str] = set()

        self._turn_usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        self._last_prompt_tokens = 0

        # Structured exit reason of the last chat() call. Set at every return
        # path so callers (delegate/cron/gateway/cli) classify outcomes by
        # attribute instead of fragile string-prefix matching on the return str.
        # Values: completed | max_iterations | interrupted | api_timeout | api_error | cancelled
        self._last_exit_reason: str | None = None

        self._interrupt_requested = False
        self._active_children = []
        self._active_children_lock = threading.Lock()

        # Steer state — non-interrupting mid-turn input. The gateway appends
        # here when the user sends a message while a turn is running; the chat
        # loop drains it at each iteration boundary and injects it as a user
        # message so the model can adjust before its next step.
        self._steer_messages: list[str] = []
        self._steer_lock = threading.Lock()

        # Subprocess registry — Popen handles spawned by this agent's tools
        # (terminal, execute_code, maven). interrupt() kills them so /stop
        # unblocks the tool's wait/communicate promptly instead of waiting for
        # the child to finish. Children's subprocesses are killed when interrupt
        # propagates to them.
        self._subprocesses: set = set()
        self._subprocesses_lock = threading.Lock()

        # 审批协调。delegate/specialist 子代理在构造处改为共享父代理的这
        # 个 dict：pending 可能挂在子代理的 turn 里，而外部（serve approve
        # 命令 / gateway steer / CLI 输入）只能拿到顶层 agent —— 共享引用
        # 让 resolve 打通到任意深度。写工具顺序执行，同时至多一个 pending。
        self._approval_shared: dict = {
            "pending": None,
            "lock": threading.Lock(),
            "request_cb": None,
            "result_cb": None,
        }

        if shared_compressor:
            self.compressor = shared_compressor
        else:
            context_length = self._get_context_length(self.model)
            self.compressor = ContextCompressor(
                context_length=context_length,
                threshold_percent=self.config["compression_threshold"],
                aux=self.aux,
            )

    def _build_system_prompt(self, platform: str = "", session_key: str = None) -> str:
        """Build system prompt from modular layers."""
        from core.prompts import build_system_prompt

        # Tools available — must use the same filter as the chat() loop's
        # get_schemas call, or conditional guidance layers advertise tools the
        # model cannot actually call.
        if self.enabled_toolsets is not None:
            _effective_ts = set(self.enabled_toolsets) | self._expansion_state
        else:
            _effective_ts = None
        available_tools = {
            s.get("function", {}).get("name")
            for s in registry.get_schemas(toolsets=_effective_ts,
                                          subagent=self.is_subagent)
        } - {None}

        # KBS protocol preamble — main agent only, injected when kbs.enabled.
        kbs_preamble = None
        agent_roster = None
        if not self.is_subagent:
            _kbs_cfg = self.config.get("kbs", {})
            if _kbs_cfg.get("enabled"):
                from core.config import AGENT_HOME
                from core.prompts import load_kbs_preamble
                kbs_preamble = load_kbs_preamble(str(AGENT_HOME / ".biz_kbs"))
            try:
                from tools.specialist_agent_tool import build_roster_prompt
                agent_roster = build_roster_prompt(available_tools) or None
            except Exception:
                logger.debug("specialist roster layer skipped", exc_info=True)

        # No subagent skips here: delegate children bypass this method entirely
        # via system_prompt_override, so the only subagents reaching the layered
        # assembly are specialist agents, which need the guidance layers that
        # match their own tools.
        prompt = build_system_prompt(
            platform=platform,
            identity_override=self.identity_override,
            available_tools=available_tools,
            skills_allowed=self.skills_allowed,
            project_context=self.project_context,
            load_claude_md=self.config.get("session", {}).get("load_claude_md", True),
            load_cursorrules=self.config.get("session", {}).get("load_cursorrules", True),
            kbs_preamble=kbs_preamble,
            kbs_read_note=self.is_subagent,
            agent_roster=agent_roster,
            cwd=self.cwd,
            language=self.config.get("language", "zh"),
        )

        # One-time debug dump of the built prompt.
        global _PRINTED_SYSTEM_PROMPT_ONCE
        if not _PRINTED_SYSTEM_PROMPT_ONCE:
            _PRINTED_SYSTEM_PROMPT_ONCE = True
            logger.info("=" * 80)
            logger.info("SYSTEM PROMPT (first build dump)")
            logger.info("=" * 80)
            logger.info(prompt)
            logger.info("=" * 80)

        return prompt

    def list_models(self) -> list[dict]:
        models_cfg = self.config.get("models", {})
        entries: dict[str, dict] = {}
        for name, meta in models_cfg.items():
            meta = meta if isinstance(meta, dict) else {}
            entries[name] = {
                "name": name,
                "context_length": (
                    meta.get("context_length")
                    or model_catalog.lookup_context_length(name, models_cfg)
                    or 128000
                ),
                "description": meta.get("description", ""),
                "source": "config",
            }
        # Live IDs from the endpoint's /models — lets /model list what the
        # provider actually offers after a base_url switch, without hand-copying
        # model names into config. Discovery failure (endpoint without /models)
        # is cached-empty inside model_catalog, so this stays cheap.
        for mid in model_catalog.discover_models(
                self.config["base_url"], self.config["api_key"]):
            if mid not in entries:
                entries[mid] = {
                    "name": mid,
                    "context_length": (
                        model_catalog.lookup_context_length(mid, models_cfg) or 128000
                    ),
                    "description": "",
                    "source": "discovered",
                }
        result = [{**e, "current": e["name"] == self.model} for e in entries.values()]
        if not result:
            result.append({
                "name": self.model,
                "context_length": self._get_context_length(self.model),
                "description": "(default)",
                "current": True,
                "source": "default",
            })
        return result

    def switch_model(self, model_name: str, session_key: str = None) -> bool:
        if session_key:
            with self._lock:
                self._session_models[session_key] = model_name
            # Persist so the override survives gateway's per-message agent
            # recreation (each message builds a fresh XiheAgent with empty
            # _session_models). Without this, /model in gateway mode doesn't stick.
            try:
                self.db.set_session_model(session_key, model_name)
            except Exception as e:
                logger.warning("Failed to persist session model: %s", e)
            logger.info("Session %s switched to %s", session_key, model_name)
        else:
            self.model = model_name
            self.aux._default_model = model_name
            new_len = self._get_context_length(model_name)
            self.compressor = ContextCompressor(
                context_length=new_len,
                threshold_percent=self.config["compression_threshold"],
                aux=self.aux,
            )
            logger.info("Global model switched to %s (context=%d)", model_name, new_len)
        return True

    def _effective_model(self, session_key: str = None) -> str:
        if session_key:
            with self._lock:
                override = self._session_models.get(session_key)
            if override:
                return override
            # Fall back to persisted per-session model (gateway mode: fresh agent
            # per message, so _session_models is empty; read what /model saved).
            try:
                persisted = self.db.get_session_model(session_key)
                if persisted:
                    return persisted
            except Exception:
                logger.warning("read persisted session model failed", exc_info=True)
        return self.model

    def interrupt(self) -> None:
        """Request the agent to stop its current tool-calling loop.

        Call from another thread (e.g., gateway message handler) to
        gracefully stop the agent. Also signals long-running tools and
        propagates to any running child agents.
        """
        self._interrupt_requested = True
        # Kill subprocesses spawned by this agent's tools so /stop unblocks
        # them promptly (their wait/communicate returns, then the loop notices
        # the flag). Subprocesses spawned by delegated children are killed when
        # propagation reaches them below.
        self.kill_subprocesses()
        with self._active_children_lock:
            for child in self._active_children:
                try:
                    child.interrupt()
                except Exception:
                    # one dead child must not stop the others from being
                    # interrupted — but a silent failure here is an
                    # undiagnosable "stop did nothing"
                    logger.warning("interrupt propagation to child failed",
                                   exc_info=True)
        logger.info("Interrupt requested for agent (depth=%d)", self.delegate_depth)

    def _check_interrupt(self) -> bool:
        """Check if an interrupt has been requested. Clears the flag."""
        if self._interrupt_requested:
            self._interrupt_requested = False
            return True
        return False

    def is_interrupted(self) -> bool:
        """Whether an interrupt has been requested for this agent (read-only).

        Does NOT clear the flag — long-running tools poll this via
        tools.interrupt.is_interrupted(), which is bound to this agent by the
        dispatch loop. Per-agent so a /stop in one chat can't be seen by a
        tool in another concurrent chat.
        """
        return bool(self._interrupt_requested)

    def steer(self, text: str) -> None:
        """Inject a non-interrupting user message into the running turn.

        Called by the gateway when the user sends a message while a turn is
        active. The chat loop drains it at the next iteration boundary (after
        the current tool/generation finishes) and appends it as a user message
        so the model can adjust before its next step — like Claude Code's
        "steer without interrupting". Thread-safe.
        """
        with self._steer_lock:
            self._steer_messages.append(text)

    def _drain_steer(self) -> list[str]:
        """Pop and return all steered messages (called at iteration boundaries)."""
        with self._steer_lock:
            msgs = self._steer_messages[:]
            self._steer_messages.clear()
            return msgs

    @property
    def pending_approval(self) -> Optional[dict]:
        """当前等待人工审批的操作（无则 None）。浅拷贝、不含 Event，
        供 serve/gateway/CLI 的路由层查询与展示。"""
        with self._approval_shared["lock"]:
            p = self._approval_shared["pending"]
            if p is None:
                return None
            return {"id": p["id"], "tool": p["tool"], "summary": p["summary"]}

    def request_approval(self, tool: str, summary: str) -> tuple[bool, str, bool]:
        """请求人工批准一个危险操作，阻塞至决议/超时/中断。

        由 dispatch 汇聚点在工具 handler 执行前调用（工具线程）。无回调
        （cron 等无人值守场景）立即拒绝，不空等超时。
        Returns (approved, reason, always)——always=True 表示用户选了
        "批准且本会话不再询问"，由调用方写入会话记忆。
        """
        shared = self._approval_shared
        cfg = (self.config.get("approvals") or {})
        try:
            timeout = float(cfg.get("timeout") or 300)
        except (TypeError, ValueError):
            timeout = 300.0
        timeout_allows = str(cfg.get("timeout_action") or "deny").strip().lower() == "allow"

        info = {"id": uuid.uuid4().hex, "tool": tool, "summary": summary}
        pending = {**info, "event": threading.Event(),
                   "approved": None, "reason": "", "always": False}
        with shared["lock"]:
            if shared["pending"] is not None:
                return False, "已有另一个待审批操作", False
            shared["pending"] = pending

        cb = shared["request_cb"]
        if cb is None:
            with shared["lock"]:
                shared["pending"] = None
            reason = "无人值守环境（无审批回调），已拒绝"
            self._notify_approval_result(info, False, reason)
            return False, reason, False
        try:
            cb(info)
        except Exception:
            logger.warning("approval request callback failed (tool=%s)", tool,
                           exc_info=True)
            with shared["lock"]:
                shared["pending"] = None
            reason = "审批通知发送失败，已拒绝"
            self._notify_approval_result(info, False, reason)
            return False, reason, False

        deadline = time.monotonic() + timeout
        while True:
            if pending["event"].wait(timeout=0.3):
                break
            if self.is_interrupted():
                pending["approved"], pending["reason"] = False, "被停止指令打断"
                break
            if time.monotonic() >= deadline:
                pending["approved"] = timeout_allows
                pending["reason"] = ("超时未确认，已按配置放行" if timeout_allows
                                     else "超时未确认，已自动拒绝")
                break

        with shared["lock"]:
            shared["pending"] = None
        approved = bool(pending["approved"])
        always = bool(pending.get("always"))
        reason = pending["reason"] or ("用户批准" if approved else "用户拒绝")
        self._notify_approval_result(info, approved, reason)
        logger.info("approval resolved: tool=%s approved=%s always=%s reason=%s",
                    tool, approved, always, reason)
        return approved, reason, always

    def resolve_approval(self, approval_id: str | None = None,
                         approved: bool = True, note: str = "",
                         always: bool = False) -> bool:
        """对当前 pending 审批下决议（serve approve 命令 / gateway steer /
        CLI 输入经 try_resolve_steer 调用）。approval_id 为空时匹配当前唯一
        pending；always=True 随"批准"一起传（"本会话不再询问"）。
        Returns True 表示决议已送达。"""
        shared = self._approval_shared
        with shared["lock"]:
            pending = shared["pending"]
            if pending is None:
                return False
            if approval_id and pending["id"] != approval_id:
                return False
            if pending["event"].is_set():
                return False
            pending["approved"] = bool(approved)
            pending["reason"] = note or ("用户批准" if approved else "用户拒绝")
            pending["always"] = bool(always)
            pending["event"].set()
            return True

    def _notify_approval_result(self, info: dict, approved: bool, reason: str):
        cb = self._approval_shared["result_cb"]
        if cb is None:
            return
        try:
            cb(info, approved, reason)
        except Exception:
            logger.warning("approval result callback failed", exc_info=True)

    def register_subprocess(self, proc) -> None:
        """Register a subprocess spawned by a tool so interrupt() can kill it."""
        with self._subprocesses_lock:
            self._subprocesses.add(proc)

    def unregister_subprocess(self, proc) -> None:
        with self._subprocesses_lock:
            self._subprocesses.discard(proc)

    def kill_subprocesses(self) -> None:
        """Kill every subprocess this agent has registered. Safe to call from
        any thread; no-op if none. Used by interrupt()."""
        with self._subprocesses_lock:
            procs = list(self._subprocesses)
            self._subprocesses.clear()
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                # keep killing the rest, but a silent failure here is an
                # undiagnosable "stop did nothing" later
                logger.warning("kill_subprocesses: kill failed for pid=%s",
                               getattr(proc, "pid", "?"), exc_info=True)

    @staticmethod
    def _get_context_length_static(config: dict, model: str) -> int:
        return model_catalog.lookup_context_length(
            model, config.get("models", {})) or 128000

    def _get_context_length(self, model: str) -> int:
        return self._get_context_length_static(self.config, model)

    def _should_compress(self, messages: list[dict]) -> bool:
        """Real usage from the last API call beats any character estimate:
        CJK-heavy prompts are undercounted ~2-3x by chars/4, which once let a
        turn sail past the threshold until the gateway answered with empty
        responses. With real usage comfortably below threshold (<80%) the
        char estimate is skipped entirely — it walks every message every
        iteration and can only disagree by overcounting there. The estimate
        still covers cold start (no usage yet) and the gray zone ≥80%, where
        one iteration's tool results could bridge the remaining margin."""
        threshold = self.compressor.threshold_tokens
        if self._last_prompt_tokens >= threshold:
            return True
        if self._last_prompt_tokens > 0 and self._last_prompt_tokens < threshold * 0.8:
            return False
        return self.compressor.should_compress(messages)

    @staticmethod
    def _notify_compressing(stream_delta_callback) -> None:
        """Compression runs a multi-second aux-LLM summary synchronously inside
        the turn — surface it as a 思考 delta so the client shows why the turn
        went quiet instead of reading as a stall."""
        if not stream_delta_callback:
            return
        try:
            stream_delta_callback(
                "[上下文接近上限，正在压缩较早的对话…]\n", kind="reasoning")
        except Exception:
            logger.debug("compression notice failed", exc_info=True)

    @staticmethod
    def _repair_dangling_tool_calls(messages: list[dict]) -> list[dict]:
        """Fix conversations where the agent crashed mid-tool-call.

        When an assistant message has tool_calls but there are no corresponding
        tool results, the API will reject the request. This method appends
        error results for any dangling tool_calls.
        """
        answered_ids = set()
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                answered_ids.add(msg["tool_call_id"])

        # Walk backwards so inserted results don't shift unprocessed indices.
        repaired = list(messages)
        i = len(repaired) - 1
        while i >= 0:
            msg = repaired[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                i -= 1
                continue

            dangling = []
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id and tc_id not in answered_ids:
                    dangling.append(tc)

            if not dangling:
                i -= 1
                continue

            for tc in dangling:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                tool_name = fn.get("name", "unknown") if isinstance(fn, dict) else "unknown"
                error_msg = {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps({
                        "error": f"Tool execution was interrupted (agent restarted). "
                                 f"Please retry if needed.",
                        "tool": tool_name,
                        "interrupted": True,
                    }),
                }
                repaired.insert(i + 1, error_msg)
                answered_ids.add(tc.get("id", ""))

            i -= 1

        return repaired

    @staticmethod
    def _inject_recovery_hint(messages: list[dict]) -> list[dict]:
        """Inject a recovery hint if the conversation was interrupted.

        If the last non-system message is a user message with no assistant
        reply, the agent crashed before responding. Add a system hint so
        the LLM knows to continue the interrupted task.
        """
        if len(messages) < 2:
            return messages

        last_msg = None
        for msg in reversed(messages):
            if msg.get("role") != "system":
                last_msg = msg
                break

        if not last_msg or last_msg.get("role") != "user":
            return messages

        if any("interrupted" in (m.get("content", "") or "") and m.get("role") == "system"
               for m in messages[-3:]):
            return messages

        hint = {
            "role": "system",
            "content": (
                "[Recovery notice] The previous conversation was interrupted "
                "(agent restarted). The user's last message was not answered. "
                "Review the conversation history above and continue the task "
                "from where it left off. Do not repeat work that was already "
                "completed."
            ),
        }
        messages.insert(len(messages) - 1, hint)
        return messages

    def chat(self, source: SessionSource, user_message: str,
             max_iterations: int = None,
             stream_delta_callback: Callable[[str], None] = None,
             tool_call_callback: Callable[[str, str, float], None] = None,
             tool_call_start_callback: Callable[[str, str], None] = None,
             tool_result_callback: Optional[Callable[[str, str, float], None]] = None,
             approval_request_callback: Optional[Callable[[dict], None]] = None,
             approval_result_callback: Optional[Callable[[dict, bool, str], None]] = None,
             approval_key: str = None) -> str:
        """Run a full conversation turn and return the final response.

        Args:
            source: SessionSource describing where the message comes from.
                    Used to generate a deterministic session key and route responses.
            stream_delta_callback: When provided, use streaming API and call
                this callback with each text delta. Used for progressive output.
            tool_call_callback: When provided, called as (tool_name, args_summary, elapsed)
                after each tool call completes. Used for CLI progress display.
            tool_call_start_callback: When provided, called as (tool_name, args_summary)
                just before each tool runs — gives immediate feedback during long ops
                instead of silence until completion.
            tool_result_callback: When provided, called as (tool_name, raw_result, elapsed)
                with the raw (pre-persist) result string after each tool completes. The
                consumer truncates as needed; receives the full output, not the context
                substitution that oversized results get replaced with.
            approval_request_callback: When provided, called with {id, tool, summary}
                when a dangerous operation needs manual approval — the mode's UI
                sends the prompt to the user. None (cron/headless) = requests are
                denied immediately instead of waiting out the timeout.
            approval_result_callback: When provided, called with
                (info, approved, reason) when the wait ends (reply/timeout/interrupt)
                so the UI can settle its approval card.
            approval_key: Overrides the approval-memory bucket for this turn
                (default: the session key). Callers with a coarser dimension
                pass their own key — cron jobs cron_job:<任务名>, workspace-
                bound serve conversations ws:<目录> — so "批准且不再询问"
                memory is shared at that dimension. History and persistence
                stay keyed by session; only the approval bucket moves.
        """
        # Stash the active callbacks so long-running tools (e.g. external_agent)
        # can stream their progress back to the user mid-turn. Only read inside
        # this loop; overwritten on the next chat() call, so no finally needed.
        self._active_stream_delta_cb = stream_delta_callback
        self._active_tool_call_cb = tool_call_callback
        self._active_tool_call_start_cb = tool_call_start_callback
        self._active_tool_result_cb = tool_result_callback
        # 审批回调进 shared dict（子代理与其共享同一引用）。只在传入非 None
        # 时覆盖：delegate/specialist 子代理的 chat() 不带回调，不能把父代理
        # 已注入的回调清掉。
        if approval_request_callback is not None:
            self._approval_shared["request_cb"] = approval_request_callback
        if approval_result_callback is not None:
            self._approval_shared["result_cb"] = approval_result_callback

        session_id = self.db.get_or_create_session(source)
        session_key = self.db.build_key(source)

        # 会话记忆按顶层会话隔离：只有顶层 chat 写键（子代理 chat 不覆盖），
        # 子代理经共享引用读到用户所在会话，记忆才对得上桶。approval_key
        # 换桶（cron 按任务名、桌面工作空间按目录），只影响审批记忆。
        if not self.is_subagent:
            self._approval_shared["session_key"] = approval_key or session_key
        effective_model = self._effective_model(session_key)

        _tool_context = {
            "chat_id": source.chat_id,
            "platform": source.platform,
            "session_key": session_key,
            "user_id": source.user_id or "",
        }

        messages = self.db.load_messages(session_id)

        messages = self._repair_dangling_tool_calls(messages)
        messages = self._inject_recovery_hint(messages)

        # Auto-title UPFRONT: fire on the session's first user message, before
        # the model replies, so the session is named even if this turn is later
        # interrupted or hits the iteration cap (the old "title after the first
        # exchange" design left interrupted first turns stuck as "新对话").
        # `messages` here is the pre-turn history, so "no user turn in it" is
        # exactly "this is the first user message". Skipped for sub-agents.
        if not self.is_subagent:
            try:
                from core.title_generator import maybe_auto_title
                maybe_auto_title(self.db, session_id, user_message, self.aux, messages)
            except Exception:
                logger.debug("auto title skipped", exc_info=True)

        # Hydrate todo store from history (gateway per-message instances lose memory state)
        if not self.is_subagent and messages:
            try:
                from tools.todo_tool import hydrate_from_history
                hydrate_from_history(messages)
            except Exception:
                logger.debug("todo hydrate skipped", exc_info=True)

        if self.system_prompt_override:
            system_prompt = self.system_prompt_override
        else:
            # Rebuilt every turn: assembly is string concat + a few file reads
            # (ms), so code/config/specialist/skill changes apply on the next
            # message without a session reset. Determinism (sorted scans, no
            # timestamps) is what keeps provider-side prefix caching hitting —
            # freezing the text in the session row was a stronger guarantee
            # than needed and pinned stale prompts.
            system_prompt = self._build_system_prompt(source.platform, session_key)
        system_msg = {"role": "system", "content": system_prompt}
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, system_msg)
        elif messages[0].get("content") != system_prompt:
            messages[0]["content"] = system_prompt

        # Recalled memory for this turn, injected into the system message at the
        # API boundary and never persisted — the stored user message must remain
        # the user's literal input.
        self._turn_memory_inject = None
        if not self.is_subagent:
            try:
                import tools.memory_tool
                self._turn_memory_inject = tools.memory_tool.build_memory_prompt() or None
            except Exception:
                logger.debug("memory prompt inject skipped", exc_info=True)
        messages.append({"role": "user", "content": user_message})

        if self._should_compress(messages):
            self._notify_compressing(stream_delta_callback)
            messages = self.compressor.compress(messages, session_key=session_key)
            # Reset file dedup cache — original content is lost after compression
            try:
                from tools.file_tools import reset_file_dedup
                reset_file_dedup()
            except Exception:
                logger.debug("file dedup reset skipped", exc_info=True)
            # Rebuild system prompt after compression (memory snapshot may be stale)
            if not self.system_prompt_override:
                new_prompt = self._build_system_prompt(source.platform, session_key)
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = new_prompt
            self.db.rewrite_messages(session_id, messages)

        # Aggregate tool-result budget applies to THIS turn's rows (this index
        # on), not just the current iteration's batch — a 40-iteration turn of
        # 2K-char results never trips a per-iteration budget. Mid-loop
        # compression shrinks the list, invalidating the index; those sites
        # reset it to 0 (post-compression history is placeholder-pruned, so
        # the widened window can't over-spill).
        turn_base = len(messages)

        self._turn_usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        self._last_prompt_tokens = 0
        self._last_exit_reason = None

        iteration = 0
        empty_retries = 0
        iter_limit = max_iterations if max_iterations is not None else self.max_iterations
        use_streaming = stream_delta_callback is not None

        while iteration < iter_limit:
            iteration += 1

            # Re-read schemas each iteration (supports mid-turn request_tools
            # expansion); registry-cached per roster + config stamp.
            if self.enabled_toolsets is not None:
                _effective_ts = set(self.enabled_toolsets) | self._expansion_state
            else:
                _effective_ts = None
            tool_schemas = registry.get_schemas(
                toolsets=_effective_ts,
                subagent=self.is_subagent,
            )
            
            if session_key and session_key.startswith("cron_"):
                from tools.cronjob_tools import is_session_cancelled, clear_cancel_flag
                if is_session_cancelled(session_key):
                    clear_cancel_flag(session_key)
                    logger.info("Session %s cancelled by user request", session_key)
                    self._last_exit_reason = "cancelled"
                    return "Job cancelled by user."

            if self._check_interrupt():
                logger.info("Agent interrupted at iteration %d", iteration)
                # Persist what we have so the next turn can recover
                self._persist_messages(session_id, messages)
                self._last_exit_reason = "interrupted"
                return "[interrupted]"

            # Inject any steered user messages (non-interrupting mid-turn input).
            # Drained at each iteration boundary so the model sees the user's
            # refinement before its next step — takes effect after the current
            # tool/generation completes (the same boundary Claude Code's steer
            # uses). Mid-generation injection isn't possible.
            _steered = self._drain_steer()
            if _steered:
                for _s in _steered:
                    messages.append({"role": "user",
                                     "content": f"[用户中途补充] {_s}"})
                self._persist_messages(session_id, messages)
                logger.info("Injected %d steered message(s) at iteration %d",
                            len(_steered), iteration)
            
            try:
                if use_streaming:
                    content, tool_calls, reasoning = self._streaming_call(
                        effective_model, messages, tool_schemas, stream_delta_callback)
                else:
                    content, tool_calls, reasoning = self._non_streaming_call(
                        effective_model, messages, tool_schemas,
                        stream_delta_callback)
            except TimeoutError as e:
                logger.error("API call timed out: %s", e)
                self._last_exit_reason = "api_timeout"
                return f"API timeout: {e}"
            except Exception as e:
                logger.error("API call failed: %s", e)
                self._last_exit_reason = "api_error"
                return f"API error: {e}"

            content = content or ""
            if content.strip().startswith("[System:") and content.strip().endswith("]"):
                content = ""
            # Honor an interrupt that landed during the generation above. The
            # streaming call breaks out mid-call (see _streaming_call); a
            # non-streaming call finishes first. Either way, stop here with the
            # partial content already produced — don't dispatch half-formed tool
            # calls or return a truncated answer as a normal completion. The
            # partial text has already been streamed to the caller, so it stays.
            # _check_interrupt() (not a raw flag read) so the flag is consumed —
            # a long-lived agent (CLI) otherwise carries it into the next turn.
            if self._check_interrupt():
                self._last_exit_reason = "interrupted"
                logger.info("Agent interrupted during generation at iteration %d", iteration)
                self._persist_messages(session_id, messages)
                return content or "[interrupted]"
            # Tool-call turns with no text: send null content (valid OpenAI format
            # for assistant tool-call messages) instead of "" — the LLM gateway
            # otherwise "sanitises" the empty string into a
            # "[System: Empty message content sanitised to satisfy protocol]" note
            # that leaks into the chat stream.
            if tool_calls and not content.strip():
                content = None
            assistant_msg = {"role": "assistant", "content": content}
            if reasoning:
                assistant_msg["_reasoning"] = reasoning

            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": tc["arguments"]}}
                    for tc in tool_calls
                ]
                messages.append(assistant_msg)

                # Signal tool boundary for streaming consumers
                if use_streaming:
                    try:
                        stream_delta_callback(None)
                    except Exception:
                        pass

                # Dispatch tool calls — order-preserving segmentation:
                # consecutive read-only calls coalesce into a parallel group
                # (reads never conflict with each other); every non-read-only
                # call runs alone, in model order, so a read AFTER a write
                # still observes the write's effect. One write in the batch
                # therefore no longer serializes the batch's independent reads.
                import time as _time
                from tools.tool_result_storage import maybe_persist_tool_result

                groups: list[list[int]] = []   # index groups into tool_calls
                groups_read: list[bool] = []
                for i, tc in enumerate(tool_calls):
                    _is_ro = registry.is_read_only(tc["name"])
                    if groups and groups_read[-1] and _is_ro:
                        groups[-1].append(i)
                    else:
                        groups.append([i])
                        groups_read.append(_is_ro)

                for g, idxs in enumerate(groups):
                    if groups_read[g] and len(idxs) > 1:
                        from concurrent.futures import ThreadPoolExecutor, as_completed
                        results_map: dict[int, str] = {}

                        def _run_tool(idx_tc):
                            idx, tc = idx_tc
                            t0 = _time.monotonic()
                            logger.info("Tool call [parallel]: %s(%s)", tc["name"],
                                        tc["arguments"][:200])
                            # Bind this agent so the tool's is_interrupted() polls
                            # THIS agent's flag — per-session, not a global event.
                            from tools.interrupt import bind_current_agent, reset_current_agent
                            _ctx_token = bind_current_agent(self)
                            try:
                                result = registry.dispatch(tc["name"], tc["arguments"],
                                                           context=_tool_context, parent_agent=self)
                            finally:
                                reset_current_agent(_ctx_token)
                            elapsed = round(_time.monotonic() - t0, 3)
                            raw_result = result if isinstance(result, str) else str(result)
                            # Layer 2: persist oversized single results
                            result = maybe_persist_tool_result(
                                content=result,
                                tool_name=tc["name"],
                                tool_use_id=tc["id"],
                            )
                            if tool_call_callback:
                                try:
                                    tool_call_callback(tc["name"], tc["arguments"][:120], elapsed)
                                except Exception:
                                    pass
                            if tool_result_callback:
                                try:
                                    tool_result_callback(tc["name"], raw_result, elapsed)
                                except Exception:
                                    pass
                            logger.info("Tool %s returned (%.3fs): %s", tc["name"], elapsed,
                                        result[:500] if isinstance(result, str) else str(result)[:500])
                            return idx, result

                        # Print tool starts up-front (main thread) so the CLI isn't
                        # silent during the parallel run; completions print as they finish.
                        if tool_call_start_callback:
                            for i in idxs:
                                _tc = tool_calls[i]
                                try:
                                    tool_call_start_callback(_tc["name"], _tc["arguments"][:120])
                                except Exception:
                                    pass

                        with ThreadPoolExecutor(max_workers=min(len(idxs), 4)) as pool:
                            futures = {pool.submit(_run_tool, (i, tool_calls[i])): i
                                       for i in idxs}
                            # Wait for ALL tools — no aggregate cutoff. Each tool
                            # governs its own timeout; interrupt (user /stop) makes
                            # responsive tools return early. A cutoff wouldn't save
                            # wall-clock anyway (the pool's shutdown(wait=True) blocks
                            # for stragglers) and would discard real results (e.g. a
                            # slow search_files) with false "timed out" errors.
                            for future in as_completed(futures):
                                try:
                                    idx, result = future.result()
                                    results_map[idx] = result
                                except Exception as exc:
                                    idx = futures[future]
                                    logger.error("Parallel tool %d failed: %s", idx, exc)
                                    tc = tool_calls[idx]
                                    results_map[idx] = tool_error(f"Tool execution failed: {exc}")

                        for i in idxs:
                            messages.append({"role": "tool", "content": results_map[i],
                                             "tool_call_id": tool_calls[i]["id"]})
                    else:
                        for i in idxs:
                            tc = tool_calls[i]
                            if tool_call_start_callback:
                                try:
                                    tool_call_start_callback(tc["name"], tc["arguments"][:120])
                                except Exception:
                                    pass
                            t0 = _time.monotonic()
                            logger.info("Tool call: %s(%s)", tc["name"],
                                        tc["arguments"][:200])
                            # Bind this agent so the tool's is_interrupted() polls
                            # THIS agent's flag — per-session, not a global event.
                            from tools.interrupt import bind_current_agent, reset_current_agent
                            _ctx_token = bind_current_agent(self)
                            try:
                                result = registry.dispatch(tc["name"], tc["arguments"],
                                                           context=_tool_context, parent_agent=self)
                            finally:
                                reset_current_agent(_ctx_token)
                            elapsed = round(_time.monotonic() - t0, 3)
                            raw_result = result if isinstance(result, str) else str(result)
                            # Layer 2: persist oversized single results
                            result = maybe_persist_tool_result(
                                content=result,
                                tool_name=tc["name"],
                                tool_use_id=tc["id"],
                            )
                            logger.info("Tool %s returned (%.3fs): %s", tc["name"], elapsed,
                                        result[:500] if isinstance(result, str) else str(result)[:500])
                            if tool_call_callback:
                                try:
                                    tool_call_callback(tc["name"], tc["arguments"][:120], elapsed)
                                except Exception:
                                    pass
                            if tool_result_callback:
                                try:
                                    tool_result_callback(tc["name"], raw_result, elapsed)
                                except Exception:
                                    pass
                            messages.append({"role": "tool", "content": result,
                                             "tool_call_id": tc["id"]})

                # Reset read-loop counter when non-read tools are called
                _read_tools = {"read_file", "search_files"}
                non_read_called = any(tc["name"] not in _read_tools for tc in tool_calls)
                if non_read_called:
                    try:
                        from tools.file_tools import notify_other_tool_call
                        notify_other_tool_call()
                    except Exception:
                        logger.debug("file dedup notify skipped", exc_info=True)

                # Layer 3: enforce aggregate budget across all tool results this turn
                from tools.tool_result_storage import enforce_turn_budget
                enforce_turn_budget(messages[turn_base:])

                # Budget pressure injection — nudge at 70%, warn at 90%
                if iter_limit > 0:
                    progress = iteration / iter_limit
                    if progress >= 0.9:
                        budget_msg = (
                            f"[BUDGET WARNING: Iteration {iteration}/{iter_limit}. "
                            f"Only {iter_limit - iteration} iteration(s) left. "
                            "Provide your final response NOW. No more tool calls unless absolutely critical.]"
                        )
                    elif progress >= 0.7:
                        budget_msg = (
                            f"[BUDGET: Iteration {iteration}/{iter_limit}. "
                            f"{iter_limit - iteration} iterations left. Start consolidating your work.]"
                        )
                    else:
                        budget_msg = None
                    if budget_msg and messages and messages[-1].get("role") == "tool":
                        last_content = messages[-1]["content"]
                        try:
                            parsed = json.loads(last_content)
                            if isinstance(parsed, dict):
                                parsed["_budget_warning"] = budget_msg
                                messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
                            else:
                                messages[-1]["content"] = last_content + f"\n\n{budget_msg}"
                        except (json.JSONDecodeError, TypeError):
                            messages[-1]["content"] = last_content + f"\n\n{budget_msg}"

                # Persist messages after each tool iteration so that
                # crash/interrupt doesn't lose in-progress work.
                self._persist_messages(session_id, messages)

                if self._should_compress(messages):
                    self._notify_compressing(stream_delta_callback)
                    messages = self.compressor.compress(messages, session_key=session_key)
                    turn_base = 0
                    # Strip stale budget warnings from compressed history
                    _strip_budget_warnings(messages)
                    if not self.system_prompt_override:
                        new_prompt = self._build_system_prompt(source.platform, session_key)
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = new_prompt
                    self.db.rewrite_messages(session_id, messages)
            else:
                # Empty content with no tool calls is a gateway/model hiccup,
                # not a completion — two known shapes: thinking models burning
                # the whole completion budget on reasoning (content starves),
                # and over-long prompts the gateway answers with empties.
                # Escalate: nudge once, then compress the context and retry
                # once more, and only then give up — persisting the warning so
                # a reloaded transcript shows the same ending the live client
                # saw (the internal nudge rows are display-filtered).
                if not content.strip() and empty_retries == 0:
                    empty_retries += 1
                    logger.warning(
                        "empty model response at iteration %d/%d — nudging to "
                        "continue (last prompt tokens=%d, reasoning chars=%d)",
                        iteration, iter_limit, self._last_prompt_tokens,
                        len(reasoning or ""))
                    messages.append(assistant_msg)
                    messages.append({"role": "user", "content": _EMPTY_NUDGE})
                    self._persist_messages(session_id, messages)
                    continue
                if not content.strip() and empty_retries == 1:
                    empty_retries += 1
                    logger.warning(
                        "second empty model response at iteration %d/%d — "
                        "compressing context before final retry (last prompt "
                        "tokens=%d)", iteration, iter_limit,
                        self._last_prompt_tokens)
                    messages.append(assistant_msg)
                    self._notify_compressing(stream_delta_callback)
                    messages = self.compressor.compress(messages, session_key=session_key)
                    turn_base = 0
                    _strip_budget_warnings(messages)
                    messages.append({"role": "user", "content": _EMPTY_NUDGE})
                    self._persist_messages(session_id, messages)
                    continue
                if not content.strip() and empty_retries:
                    content = _EMPTY_RESPONSE_WARNING
                    assistant_msg["content"] = content
                messages.append(assistant_msg)
                self._persist_messages(session_id, messages)

                self._log_turn_usage(effective_model, session_key,
                                     session_id=session_id)
                self._last_exit_reason = "completed"
                return content or ""

        self._persist_messages(session_id, messages)
        self._log_turn_usage(effective_model, session_key, session_id=session_id)
        self._last_exit_reason = "max_iterations"
        return (f"已达到单轮处理上限（本轮共 {iter_limit} 次迭代，任务可能未完成）。"
                f"中间过程已保存，可让我继续，或换个思路重试。")

    @staticmethod
    def _is_retryable_error(e) -> bool:
        """Check if an API error is worth retrying."""
        status = getattr(e, 'status_code', None)
        if status in (429, 500, 502, 503, 504):
            return True
        if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
            return e.response.status_code in (429, 500, 502, 503, 504)
        msg = str(e).lower()
        return any(kw in msg for kw in ('rate limit', 'overloaded', 'timeout', '503', '502', '500'))

    def _call_with_retry(self, fn, max_retries=3, timeout=120.0):
        """Call fn with exponential backoff + jitter on retryable errors.

        Timeout is handled by the OpenAI SDK's native httpx timeout (set on
        the client via ``timeout=httpx.Timeout(120, connect=10)``). This
        method only handles *retryable* errors (5xx, rate-limit, timeouts).
        """
        import random
        import time
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as e:
                # OpenAI SDK raises openai.APITimeoutError on timeout
                is_timeout = isinstance(e, (TimeoutError,)) or 'timeout' in str(e).lower()
                if is_timeout:
                    logger.error("API call timed out (attempt %d/%d): %s",
                                 attempt + 1, max_retries + 1, e)
                    if attempt >= max_retries:
                        raise TimeoutError(f"API call timed out after {max_retries + 1} attempts") from e
                    delay = min(2 ** attempt, 8) + random.uniform(0, 1)
                    logger.warning("Retrying after timeout in %.1fs", delay)
                    time.sleep(delay)
                    continue

                if attempt >= max_retries or not self._is_retryable_error(e):
                    raise
                delay = min(2 ** attempt, 8) + random.uniform(0, 1)
                logger.warning("API error (attempt %d/%d): %s — retrying in %.1fs",
                               attempt + 1, max_retries + 1, e, delay)
                time.sleep(delay)

    def _non_streaming_call(self, model, messages, tool_schemas,
                            stream_delta_callback=None) -> tuple[str, list[dict], str]:
        """Standard non-streaming API call. Returns (content, tool_calls, reasoning)."""
        api_messages = self._prepare_api_messages(messages)
        def _call():
            kwargs = dict(model=model,
                          messages=api_messages,
                          max_tokens=self.max_completion_tokens)
            if tool_schemas:
                kwargs["tools"] = [{"type": "function", "function": s["function"]}
                                   for s in tool_schemas]
            if self.request_extra:
                kwargs["extra_body"] = self.request_extra
            return self.client.chat.completions.create(**kwargs)
        response = self._call_with_retry(_call)
        choice = response.choices[0]
        content = choice.message.content or ""

        # Capture reasoning_content (GLM/DeepSeek thinking); pass to callback, else log.
        reasoning = getattr(choice.message, "reasoning_content", None)
        if reasoning:
            if stream_delta_callback:
                try:
                    stream_delta_callback(reasoning, kind="reasoning")
                except TypeError:
                    try:
                        stream_delta_callback(reasoning)
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                logger.debug("[reasoning] %s", reasoning[:500])

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                })
        self._record_usage(response)
        return content, tool_calls, reasoning or ""

    def _prepare_api_messages(self, messages: list[dict]) -> list[dict]:
        """Return an API-only copy of ``messages`` with ephemeral injections that
        never reach session history: memory-context appended to the system
        message, a prefill assistant message, and underscore-prefixed internal
        keys (e.g. ``_reasoning``) stripped."""
        # Copy-elision: skip the per-message rebuild when no internal keys are
        # present (the common history shape) — the list and its dicts are only
        # read by the SDK from here on.
        if any(k.startswith("_") for m in messages for k in m):
            messages = [{k: v for k, v in m.items() if not k.startswith("_")}
                        for m in messages]

        memory_block = getattr(self, "_turn_memory_inject", None)
        if memory_block and messages and messages[0].get("role") == "system":
            messages = list(messages)
            sys0 = messages[0]
            messages[0] = {
                **sys0,
                "content": (
                    f"{sys0.get('content', '')}\n\n"
                    f"<memory-context>\n"
                    f"[System note: The following is recalled memory context, "
                    f"NOT new user input. Treat as informational background data.]\n\n"
                    f"{memory_block}\n"
                    f"</memory-context>"
                ),
            }

        prefill_cfg = self.config.get("prefill", {})
        if not prefill_cfg:
            return messages

        has_assistant = any(m.get("role") == "assistant" for m in messages)
        if has_assistant:
            return messages

        text = prefill_cfg.get("text", "")
        if not text:
            return messages

        result = list(messages)
        result.append({"role": "assistant", "content": text})
        return result

    def _streaming_call(self, model, messages, tool_schemas,
                        stream_delta_callback) -> tuple[str, list[dict], str]:
        """Streaming API call. Returns (content, tool_calls, reasoning)."""
        api_messages = self._prepare_api_messages(messages)
        def _call():
            kwargs = dict(model=model,
                          messages=api_messages,
                          max_tokens=self.max_completion_tokens,
                          stream=True,
                          stream_options={"include_usage": True})
            if tool_schemas:
                kwargs["tools"] = [{"type": "function", "function": s["function"]}
                                   for s in tool_schemas]
            if self.request_extra:
                kwargs["extra_body"] = self.request_extra
            return self.client.chat.completions.create(**kwargs)

        stream = self._call_with_retry(_call)

        content_parts = []
        reasoning_parts = []
        # arguments accumulate as chunk lists (joined once below) — string +=
        # over hundreds of argument deltas is quadratic
        tool_calls_acc = {}  # index -> {id, name, arguments: list[str]}

        for chunk in stream:
            # Honor a mid-generation interrupt: stop consuming the stream and
            # return whatever content arrived so far. interrupt() can't cancel
            # the in-flight HTTP request, but breaking here drops the
            # connection server-side and lets the caller react instantly
            # instead of waiting for the model to finish a long answer. Partial
            # tool-call deltas are discarded — they'd be malformed if dispatched.
            if self._interrupt_requested:
                try:
                    stream.close()
                except Exception:
                    pass
                logger.info("Streaming interrupted mid-generation; %d content chars kept",
                            len("".join(content_parts)))
                tool_calls_acc.clear()
                break
            # Capture usage from final chunk (choices empty, usage present)
            if hasattr(chunk, 'usage') and chunk.usage:
                self._record_usage_obj(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                try:
                    stream_delta_callback(reasoning_delta, kind="reasoning")
                except TypeError:
                    # Callback doesn't support kind parameter — treat as content
                    try:
                        stream_delta_callback(reasoning_delta)
                    except Exception:
                        pass
                except Exception:
                    pass

            if delta.content:
                dc = delta.content
                if dc.strip().startswith("[System:") and dc.strip().endswith("]"):
                    dc = ""
                if dc:
                    content_parts.append(dc)
                    try:
                        stream_delta_callback(dc, kind="content")
                    except TypeError:
                        try:
                            stream_delta_callback(dc)
                        except Exception:
                            pass
                    except Exception:
                        pass

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": "", "name": "", "arguments": [],
                        }
                    if tc_delta.id:
                        tool_calls_acc[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_acc[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"].append(
                                tc_delta.function.arguments)

        content = "".join(content_parts)
        if content and content.strip().startswith("[System:") and content.strip().endswith("]"):
            content = ""
        tool_calls = []
        for idx in sorted(tool_calls_acc.keys()):
            acc = tool_calls_acc[idx]
            tool_calls.append({
                "id": acc["id"],
                "name": acc["name"],
                "arguments": "".join(acc["arguments"]),
            })

        return content, tool_calls, "".join(reasoning_parts)

    def _record_usage(self, response):
        """Extract and accumulate token usage from an API response."""
        usage = getattr(response, 'usage', None)
        if usage:
            self._record_usage_obj(usage)

    def _record_usage_obj(self, usage):
        """Accumulate a usage object into per-turn totals."""
        try:
            p = getattr(usage, 'prompt_tokens', 0) or 0
            c = getattr(usage, 'completion_tokens', 0) or 0
            self._turn_usage["prompt"] += p
            self._turn_usage["completion"] += c
            self._turn_usage["total"] += p + c
            self._turn_usage["calls"] += 1
            self._last_prompt_tokens = p
        except Exception:
            pass

    def _log_turn_usage(self, model: str, session_key: str = "",
                        session_id: str = None):
        """Log per-turn token summary, persist it onto the turn's final
        assistant row, and update daily aggregation."""
        u = self._turn_usage
        if u["calls"] == 0:
            return
        if session_id:
            try:
                self.db.set_last_assistant_usage(
                    session_id, {"prompt": u["prompt"], "completion": u["completion"],
                                 "total": u["total"], "calls": u["calls"]})
            except Exception:
                # display-only badge — never fail the turn over it
                logger.warning("persist turn usage failed", exc_info=True)
        logger.info(
            "Token usage [turn]: model=%s calls=%d prompt=%d completion=%d total=%d",
            model, u["calls"], u["prompt"], u["completion"], u["total"],
        )
        try:
            import json as _json
            from pathlib import Path
            from datetime import datetime as _dt
            from core.config import AGENT_HOME
            usage_file = AGENT_HOME / "usage.json"
            usage_file.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if usage_file.exists():
                try:
                    data = _json.loads(usage_file.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            today = _dt.now().strftime("%Y-%m-%d")
            day = data.setdefault(today, {})
            m = day.setdefault(model, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})
            m["prompt"] += u["prompt"]
            m["completion"] += u["completion"]
            m["total"] += u["total"]
            m["calls"] += u["calls"]
            usage_file.write_text(_json.dumps(data, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to update daily usage: %s", e)

    def _persist_messages(self, session_id: str, messages: list[dict]):
        self.db.rewrite_messages(session_id, messages)
