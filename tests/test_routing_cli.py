"""CLI 装配层路由评估 — 进程内走 xihe chat 同一条装配路径。

比 tests/test_routing_eval.py（手搓核心件）多覆盖的真实环节：真实
SharedContext（SessionDB 连真实 sessions.db、MCP 发现、aux/compressor、
cron 调度器、config.yaml main roster 解析）+ 真实持久化 transcript。
与 `xihe chat -q` 的差别只剩 argparse/stdout 壳 —— init_agent 的函数体
就是本模块 fixture 复刻的两行（SharedContext + create_agent）。

    XIHE_EVAL_LLM=1 python -m pytest tests/test_routing_cli.py -v -s   # 全量
    XIHE_EVAL_LLM=1 python -m pytest tests/test_routing_cli.py -k S8   # 单场景

判定数据主源 = agent.chat 的 tool_call_start_callback；真实 sessions.db
的 transcript（platform='cli', chat_id='eval-cli-<id>'）作持久化交叉校验
（会话行缺失 = error）。跑完按 chat_id 前缀精确删除这些 eval 会话，不动
用户其它会话。cron 调度器随 SharedContext 启动 —— 与真实 xihe chat 进程
行为一致，正是本模块要的保真度。
"""
import json
import logging
import os
import sqlite3
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from test_routing_eval import (  # noqa: F401  (scenario matrix shared)
    EVAL_DIR, SANDBOX, SCENARIOS, _PingHandler, _bucket, _make_fixtures,
)

CLI_REPORT_MD = EVAL_DIR / "routing-cli-report.md"
CLI_REPORT_JSON = EVAL_DIR / "routing-cli-report.json"
_CHAT_PREFIX = "eval-cli-"

pytestmark = pytest.mark.skipif(
    not os.environ.get("XIHE_EVAL_LLM"),
    reason="real-LLM routing eval — opt in with XIHE_EVAL_LLM=1",
)

RESULTS: list = []


@pytest.fixture(scope="module")
def cli_env():
    EVAL_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(EVAL_DIR / "eval-cli.log"), filemode="w",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from core.config import load_config
    from cli.app import SharedContext

    _make_fixtures()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # init_agent 的函数体。每场景再 ctx.create_agent（gateway 每消息同款，
    # 也避免 N 个 SharedContext 起 N 个 cron 调度器）。SessionDB 在此连真实
    # sessions.db —— 模块级 fixture 先于 conftest 的函数级 isolate_db 执行。
    ctx = SharedContext(load_config())

    ns = type("CliEnv", (), {})()
    ns.ctx = ctx
    ns.ping_url = f"http://127.0.0.1:{server.server_address[1]}/ping"
    yield ns

    server.shutdown()
    if len(RESULTS) == len(SCENARIOS):
        _write_report()
    _cleanup_sessions()


def _real_db() -> Path:
    from core.config import AGENT_HOME
    return Path(AGENT_HOME) / "sessions" / "sessions.db"


def _read_db_tools(chat_id: str):
    """真实 sessions.db 持久化交叉校验；返回 (tools|None, final|None)。"""
    db_path = _real_db()
    if not db_path.exists():
        return None, None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, None
    try:
        row = db.execute(
            "SELECT session_id FROM sessions WHERE platform='cli' AND chat_id=?",
            (chat_id,)).fetchone()
        if not row:
            return None, None
        rows = db.execute(
            "SELECT role, content, tool_calls FROM messages "
            "WHERE session_id=? ORDER BY id", (row[0],)).fetchall()
    except sqlite3.Error:
        return None, None
    finally:
        db.close()
    tools, final = [], None
    for role, content, tool_calls in rows:
        if role != "assistant":
            continue
        if tool_calls:
            try:
                for tc in json.loads(tool_calls):
                    tools.append(tc["function"]["name"])
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        if content:
            final = content
    return tools, final


def _cleanup_sessions():
    db_path = _real_db()
    if not db_path.exists():
        return
    try:
        db = sqlite3.connect(str(db_path))
        sids = [r[0] for r in db.execute(
            "SELECT session_id FROM sessions WHERE platform='cli' "
            "AND chat_id LIKE ?", (_CHAT_PREFIX + "%",)).fetchall()]
        for sid in sids:
            db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        db.execute("DELETE FROM sessions WHERE platform='cli' AND chat_id "
                   "LIKE ?", (_CHAT_PREFIX + "%",))
        db.commit()
        db.close()
    except sqlite3.Error:
        pytest.fail("eval session cleanup failed on real sessions.db",
                    pytrace=False)


def run_cli_scenario(ctx, sc, ping_url: str) -> dict:
    from core.session import SessionSource

    chat_id = _CHAT_PREFIX + sc["id"].lower()
    source = SessionSource(platform="cli", chat_id=chat_id, chat_type="dm")
    prompt = sc["prompt"].replace("{ping_url}", ping_url)

    agent = ctx.create_agent(enabled_toolsets=ctx.main_toolsets,
                             skills_allowed=ctx.main_skills, cwd=str(SANDBOX))
    calls: list[str] = []
    watchdog = threading.Timer(sc["timeout"], agent.interrupt)
    t0 = time.monotonic()
    final, err, exit_reason, usage = "", None, None, {}
    try:
        watchdog.start()
        final = agent.chat(
            source, prompt,
            tool_call_start_callback=lambda n, a: calls.append(n),
        )
        exit_reason = getattr(agent, "_last_exit_reason", None)
        usage = dict(getattr(agent, "_turn_usage", {}))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        watchdog.cancel()
    elapsed = round(time.monotonic() - t0, 1)

    db_tools, db_final = _read_db_tools(chat_id)
    names = calls
    called = set(names)
    try:
        verdict = sc["judge"](called, len(names)) if not err else "error"
    except TypeError:
        verdict = sc["judge"](called) if not err else "error"
    if db_tools is None:
        verdict = "error"
        err = err or "session row missing in real sessions.db (persistence)"
    if exit_reason == "interrupted":
        verdict = f"timeout({verdict})"
    return {
        "id": sc["id"], "ladder": sc["ladder"], "prompt": prompt,
        "expect": sc["expect"], "verdict": verdict, "error": err,
        "exit_reason": exit_reason, "elapsed_s": elapsed,
        "tool_calls": names, "db_tool_calls": db_tools,
        "usage": usage,
        "final_text": (final or db_final or "")[:800],
    }


def _write_report():
    results = RESULTS
    counts = {}
    for r in results:
        counts[_bucket(r["verdict"])] = counts.get(_bucket(r["verdict"]), 0) + 1

    CLI_REPORT_JSON.write_text(
        json.dumps({"mode": "in-process SharedContext (xihe chat assembly)",
                    "results": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = [
        "# 路由评估报告 — CLI 装配层（进程内 SharedContext）", "",
        f"- 判分: **{counts.get('pass', 0)} pass / {counts.get('partial', 0)} "
        f"partial / {counts.get('fail', 0) + counts.get('error', 0)} fail**"
        f"（超时 {counts.get('timeout', 0)}，观察项不计）", "",
        "| # | 阶梯 | 预期 | 实际工具序列 | DB 持久化 | 判定 | 耗时 | tokens(↑/↓) |",
        "|---|------|------|--------------|-----------|------|------|--------------|",
    ]
    for r in results:
        u = r.get("usage") or {}
        # Subsequence, not equality: the callback stream includes SUBAGENT
        # tool calls (they transit the parent callback), while the parent's
        # DB transcript stores only its own tool_calls.
        db = r.get("db_tool_calls") or []
        it = iter(r.get("tool_calls") or [])
        db_ok = "✓" if all(t in it for t in db) else "✗"
        lines.append(
            f"| {r['id']} | {r['ladder']} | {r['expect']} | "
            f"{' → '.join(r['tool_calls']) or '(无工具)'} | {db_ok} "
            f"| **{r['verdict']}** | {r['elapsed_s']}s "
            f"| {u.get('prompt', 0)}/{u.get('completion', 0)} |")
    lines += ["", "## 各场景最终回复（截断）", ""]
    for r in results:
        lines += [f"### {r['id']} — {r['verdict']}", "",
                  f"> prompt: {r['prompt']}", "", r["final_text"] or "(空)", ""]
        if r["error"]:
            lines += [f"error: {r['error']}", ""]
    CLI_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


class TestRoutingCliLadder:
    @pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s["id"])
    def test_scenario(self, cli_env, sc):
        r = run_cli_scenario(cli_env.ctx, sc, cli_env.ping_url)
        RESULTS.append(r)
        print(f"[{sc['id']}] {r['verdict']}  tools={' → '.join(r['tool_calls'])} "
              f"elapsed={r['elapsed_s']}s", flush=True)

        verdict = str(r["verdict"])
        inner = verdict[len("timeout("):-1] if verdict.startswith("timeout(") \
            else verdict
        if inner in ("pass", "observed"):
            return
        if inner == "partial":
            pytest.xfail(f"主直做未委派（已知残留）: {' → '.join(r['tool_calls'])}")
        raise AssertionError(
            f"{sc['id']} 路由不符预期（{sc['expect']}）: "
            f"{' → '.join(r['tool_calls']) or '(无工具)'}")
