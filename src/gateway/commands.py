"""Shared slash commands — used by both CLI and gateway.

Each command handler receives (args, ctx) where:
  - args: str — the text after the command (e.g., "glm-5-tc" for "/model glm-5-tc")
  - ctx: dict — {"agent": XiheAgent, "session_key": str, "platform_adapter": ...}
"""

import os
import sys
import logging

logger = logging.getLogger(__name__)


# Natural-language + slash triggers that mean "stop the running task".
# Matched against the WHOLE message (after trimming and stripping trailing
# punctuation) so they don't fire on substrings inside normal sentences like
# "停止监控" or "how do I stop a service". /stop stays the exact, guaranteed
# trigger; the phrases below are convenience shortcuts.
_STOP_PHRASES = {
    # Chinese
    "停", "停止", "停下", "停下来", "停手", "停一下",
    "停止吧", "停下吧", "停下来吧", "请停", "请停止", "请停下",
    "取消", "取消吧", "请取消", "打住",
    "中断", "终止", "中止",
    "结束", "结束吧", "请结束", "结束了",   # end / finish
    "完了", "完事", "完事儿",              # colloquial "done"
    "算了", "不要了", "别做了", "别弄了",
    # English
    "stop", "stop it", "cancel", "abort", "halt", "quit",
    "done", "finish", "finished",
    "never mind", "nevermind", "nvm",
}
# For robust natural-language intent, route through a cheap classifier (future work).


def is_stop_intent(text: str) -> bool:
    """True if `text` asks to stop the running task.

    Recognizes the /stop and /cancel slash commands, or a short natural-
    language phrase (停 / 停止 / 取消 / stop / cancel / ...). Matched against
    the whole message (after trimming and stripping trailing punctuation) so
    ordinary sentences containing these words don't trigger.
    """
    if not text or not text.strip():
        return False
    t = text.strip()
    head = t.split(maxsplit=1)[0].lower()
    if head in ("/stop", "/cancel"):
        return True
    return t.rstrip("。.!！?？~…,，;；").strip().lower() in _STOP_PHRASES


def _get_session_id(agent, sk: str) -> str:
    """Get session_id from session_key, creating if needed."""
    entry = agent.db.get_entry(sk)
    if entry:
        return entry.session_id
    row = agent.db._conn.execute(
        "SELECT session_id FROM sessions WHERE session_key = ?", (sk,)
    ).fetchone()
    return row[0] if row else None


def handle_command(text: str, ctx: dict) -> str | None:
    """Parse and execute a slash command. Returns response string or None."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    agent = ctx.get("agent")
    sk = ctx.get("session_key", "default")
    adapter = ctx.get("platform_adapter")

    if cmd in ("/new", "/reset"):
        agent.db.reset_session(sk)
        return "Session reset."

    elif cmd in ("/stop", "/cancel"):
        # Interrupt the active turn without starting a new one. The adapter
        # usually intercepts stop intents out-of-band before they reach here;
        # this is the fallback (CLI, or platforms that route /stop as a message).
        from gateway.bot import interrupt_session
        if interrupt_session(sk):
            logger.info("User requested stop for session %s", sk)
            return "⏹ 已发送停止信号，正在中断当前任务..."
        return "没有正在运行的任务。"

    elif cmd == "/title":
        session_id = _get_session_id(agent, sk)
        if not args:
            title = agent.db.get_session_title(session_id) or "(untitled)" if session_id else "(no session)"
            return f"Current title: {title}"
        if session_id:
            agent.db.set_session_title(session_id, args.strip())
            return f"Title set: {args.strip()}"
        return "No active session."

    elif cmd == "/help":
        return "\n".join([
            "直接用自然语言下任务即可；命令列表：",
            "",
            "常用:",
            "/new, /reset      - 重置会话",
            "/stop             - 停止当前任务（也可直接说：停 / 停止 / 取消）",
            "/status           - 会话与模型信息",
            "/tools            - 查看可用工具",
            "/model [name]     - 切换模型（无参数 = 列表）",
            "/sessions         - 历史会话列表",
            "/resume [<n|名>]  - 恢复某个会话（仅 CLI）",
            "",
            "进阶:",
            "/title [text]     - 查看/设置会话标题",
            "/history [N]      - 查看历史消息（默认 20 条）",
            "/compress         - 手动压缩上下文",
            "/login <system>   - 浏览器登录某系统（如 /login wiki）",
            "/reload-mcp       - 重载 MCP 服务器",
            "/ping             - 测试模型连接",
            "/clear            - 清屏（仅 CLI）",
            "/quit, /exit      - 退出（仅 CLI）",
            "/help             - 显示本帮助",
        ])

    elif cmd == "/model":
        if not args:
            models = agent.list_models()
            lines = ["Available models:"]
            for m in models:
                marker = " *" if m["current"] else ""
                desc = f" ({m['description']})" if m["description"] else ""
                lines.append(f"  {m['name']}{desc}{marker}")
            lines.append(f"\nCurrent: {agent._effective_model(sk)}")
            return "\n".join(lines)

        model_name = args.strip()
        available = {m["name"] for m in agent.list_models()}
        if available and model_name not in available:
            return f"Unknown: {model_name}\nAvailable: {', '.join(sorted(available))}"

        agent.switch_model(model_name, session_key=sk)
        ctx_len = agent._get_context_length(model_name)
        return f"Switched to {model_name} (context: {ctx_len // 1000}K)"

    elif cmd == "/status":
        model = agent._effective_model(sk)
        ctx_len = agent._get_context_length(model)
        platform_name = adapter.name if adapter else "cli"
        session_id = _get_session_id(agent, sk)
        title = agent.db.get_session_title(session_id) or "(untitled)" if session_id else "(no session)"
        lines = [
            f"Session: {sk}",
            f"Title: {title}",
            f"Platform: {platform_name}",
            f"Model: {model}",
            f"Context: {ctx_len // 1000}K",
        ]
        # Show tool count
        from tools import registry
        active = sum(1 for t in registry._tools.values() if not t.check_fn or t.check_fn())
        lines.append(f"Tools: {active} active")
        # Scheduler health
        try:
            from tools.cronjob_tools import scheduler_health
            health = scheduler_health()
            if health["running"]:
                tick = f"{health['last_tick_ago']}s ago" if health["last_tick_ago"] else "n/a"
                lines.append(f"Scheduler: alive (tick {tick})")
            else:
                lines.append("Scheduler: stopped")
        except Exception:
            pass
        return "\n".join(lines)

    elif cmd == "/history":
        session_id = _get_session_id(agent, sk)
        if not session_id:
            return "No conversation history."
        messages = agent.db.load_messages(session_id)
        if not messages:
            return "No conversation history."
        # /history [N] — how many recent messages to show (default 20; caps
        # gateway message-length truncation). e.g. /history 5, /history 100.
        limit = 20
        if args.strip().isdigit():
            limit = max(1, min(int(args.strip()), 200))
        lines = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "system":
                continue
            preview = content[:100].replace("\n", " ")
            if len(content) > 100:
                preview += "..."
            lines.append(f"[{role}] {preview}")
        if not lines:
            return "No conversation history."
        return "\n".join(lines[-limit:])

    elif cmd == "/sessions":
        # Scope to the current user's sessions (gateway sets user_id=sender_id);
        # CLI sessions have user_id=None → no filter (single-user admin view).
        entry = agent.db.get_entry(sk)
        cur_user = entry.origin.user_id if (entry and entry.origin) else None
        rows = agent.db.list_sessions(limit=30, user_id=cur_user)
        if not rows:
            return "No sessions."
        lines = ["Sessions (most recent first):"]
        for i, r in enumerate(rows, 1):
            title = r.get("title") or "(untitled)"
            ts = (r.get("updated_at") or "")[:16].replace("T", " ")
            name = r.get("chat_id") or r.get("session_key", "")
            plat = r.get("platform") or "?"
            lines.append(f"  {i:2d}. [{plat}] {name} — {title}  ({ts}, {r.get('msg_count', 0)} msgs)")
        lines.append(f"\n{len(rows)} session(s); internal cron/delegate sessions hidden. CLI: resume with `xihe chat -r`.")
        return "\n".join(lines)

    elif cmd == "/resume":
        # CLI-only: switch the active session mid-REPL (gateway sessions are
        # bound to the incoming chat_id, so switching is meaningless there).
        if ctx.get("platform_adapter"):
            return "/resume 仅在 CLI 可用。"
        # Only sessions with a real chat_id are resumable (CLI resume rebuilds
        # the key from chat_id); legacy rows with empty chat_id can't switch.
        rows = [r for r in agent.db.list_sessions(limit=30, platform="cli") if r.get("chat_id")]
        if not rows:
            return "没有可恢复的 CLI 会话。"

        def _resolve(choice):
            if choice.isdigit() and 1 <= int(choice) <= len(rows):
                return rows[int(choice) - 1].get("chat_id")
            return next((r.get("chat_id") for r in rows if r.get("chat_id") == choice), None)

        if args.strip():
            target = _resolve(args.strip())
            if not target:
                return f"未找到 '{args.strip()}'(可恢复 {len(rows)} 条)。用法:/resume(选号)或 /resume <序号/名字>。"
        else:
            print("最近的 CLI 会话 — 选序号切换(回车取消):")
            for i, r in enumerate(rows, 1):
                title = r.get("title") or "(未命名)"
                ts = (r.get("updated_at") or "")[:16].replace("T", " ")
                name = r.get("chat_id") or r.get("session_key", "")
                print(f"  {i:2d}. {name} — {title}  ({ts}, {r.get('msg_count', 0)} msgs)")
            try:
                choice = input("resume> ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not choice:
                return None
            target = _resolve(choice)
            if not target:
                return f"未找到 '{choice}'(可恢复 {len(rows)} 条)。"

        from core.session import SessionSource
        new_source = SessionSource(platform="cli", chat_id=target, chat_type="dm")
        new_key = agent.db.build_key(new_source)
        ctx["cli_source"] = new_source
        ctx["session_key"] = new_key
        entry = agent.db.get_entry(new_key)
        title = agent.db.get_session_title(entry.session_id) if entry else None
        return f"✅ 已切换到 [{target}] {title or '(未命名)'}。下一条消息起在这段历史里继续。"

    elif cmd == "/compress":
        session_id = _get_session_id(agent, sk)
        if not session_id:
            return "No messages to compress."
        messages = agent.db.load_messages(session_id)
        if not messages:
            return "No messages to compress."
        if agent.compressor.should_compress(messages):
            compressed = agent.compressor.compress(messages, session_key=sk)
            agent.db.rewrite_messages(session_id, compressed)
            return f"Compressed: {len(messages)} -> {len(compressed)} messages."
        return "No compression needed (context within limits)."

    elif cmd == "/tools":
        from tools import registry
        lines = ["Available tools:"]
        for entry in sorted(registry._tools.values(), key=lambda e: e.name):
            check_fn = entry.check_fn
            active = not check_fn or check_fn()
            status = "ON" if active else "OFF"
            desc = entry.schema.get("function", {}).get("description", "") if "function" in entry.schema else ""
            short_desc = desc.split("\n")[0][:60] if desc else ""
            lines.append(f"  {entry.name:20s} [{status}] {short_desc}")
        return "\n".join(lines)

    elif cmd == "/clear":
        # Only meaningful in CLI — handled by caller
        return "__CLEAR__"

    elif cmd in ("/quit", "/exit", "/q"):
        return "__QUIT__"

    elif cmd == "/ping":
        return "pong"

    elif cmd == "/login":
        system = args.strip()
        if not system:
            return "Usage: /login <system>\nExample: /login wiki"
        # Return None to let the message flow through to agent.chat()
        # The agent will use browser_login + browser_type/browser_click
        # to help the user log in interactively.
        return None

    elif cmd == "/reload-mcp":
        # Full reload of MCP servers from config (user + project merged). Handles
        # add/remove/change without restarting the gateway. discover_mcp_tools()
        # is idempotent; the live registry means the next message's fresh agent
        # picks up the new toolset automatically — no cached-agent refresh needed.
        from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, get_mcp_status
        try:
            shutdown_mcp_servers()
            discover_mcp_tools()
        except Exception as e:
            return f"MCP reload failed: {e}"
        status = get_mcp_status()
        connected = [s for s in status if s.get("connected")]
        total_tools = sum(s.get("tools", 0) for s in connected)
        msg = f"MCP reloaded: {len(connected)}/{len(status)} server(s) connected, {total_tools} tool(s)."
        offline = [s["name"] for s in status if not s.get("connected")]
        if offline:
            msg += f" Offline: {', '.join(offline)}"
        return msg

    return None
