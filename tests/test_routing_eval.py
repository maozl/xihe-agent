"""三层 agent 路由评估 — 真实 LLM 的 pytest 形态。

默认整体跳过（真 token 成本、单场景分钟级，不该混进常规 19s 套件）：

    XIHE_EVAL_LLM=1 python -m pytest tests/test_routing_eval.py -v -s   # 全量
    XIHE_EVAL_LLM=1 python -m pytest tests/test_routing_eval.py -k S8   # 单场景

与桌面 serve 同构（resolve_roster → merge_mounts → XiheAgent），但绕开
SharedContext——它顺带启动 cron 调度器，会把用户真实定时任务拉进本进程。
产物全部落在 tests/evals/（已 gitignore）：报告、日志、沙箱夹具、独立
SQLite（每次运行重建，不碰真实 sessions.db；conftest 的 isolate_db 是函数级
autouse，罩不住本模块的模块级 fixture，所以这里自己改 _DB_PATH）。报告只在
全量跑完时写，子集运行只在 stdout 出判定，避免半份报告覆盖全量报告。
"""
import json
import logging
import os
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent / "evals"
SANDBOX = EVAL_DIR / "sandbox"
_SANDBOX_POSIX = SANDBOX.as_posix()
REPORT_JSON = EVAL_DIR / "routing-report.json"
REPORT_MD = EVAL_DIR / "routing-report.md"
EVAL_DB = EVAL_DIR / "eval-sessions.db"

pytestmark = pytest.mark.skipif(
    not os.environ.get("XIHE_EVAL_LLM"),
    reason="real-LLM routing eval — opt in with XIHE_EVAL_LLM=1",
)

_DELEGATION_TOOLS = {"run_itsm_agent", "run_claude_agent", "delegate_task",
                     "external_agent"}

API_MD = """# 用户中心 API 说明

## POST /api/v1/users
创建新用户。请求体包含 username、email、department 三个字段，
均为必填。username 长度限制 4-32 字符，email 需为公司邮箱后缀。
返回 201 和新建用户的完整 JSON。

## GET /api/v1/users/{id}
查询单个用户详情。用户不存在时返回 404。响应包含用户的基本信息、
所属部门、最近一次登录时间和账号状态。

## PUT /api/v1/users/{id}
更新用户信息。可更新字段与创建接口一致，均为可选。
至少需要提供一个字段，否则返回 400。

## DELETE /api/v1/users/{id}
删除用户。软删除，数据保留 90 天。重复删除返回 409。

## 错误码约定
- 400 参数格式错误
- 401 未携带有效 token
- 403 无权限操作目标部门
- 404 资源不存在
- 429 触发限流，间隔 60s 后重试
"""

CALC_PY = '''"""Calculator module (intentionally bloated for the refactor scenario)."""


def calculate_and_report(a, b, op, log_path=None):
    """Do way too many things: parse op, compute, format a report, and
    (optionally) write it to a file. Kept as one long function on purpose."""
    import datetime
    if op == "add":
        result = a + b
    elif op == "sub":
        result = a - b
    elif op == "mul":
        result = a * b
    elif op == "div":
        if b == 0:
            result = None
            error = "division by zero"
        else:
            result = a / b
            error = None
    else:
        result = None
        error = f"unknown op: {op}"
    if result is None and error is None:
        error = "no result"
    lines = []
    lines.append("=" * 40)
    lines.append("CALCULATION REPORT")
    lines.append("=" * 40)
    lines.append(f"time    : {datetime.datetime.now().isoformat()}")
    lines.append(f"operands: a={a} b={b}")
    lines.append(f"op      : {op}")
    lines.append(f"result  : {result}")
    if error:
        lines.append(f"error   : {error}")
    lines.append("=" * 40)
    report = "\\n".join(lines)
    if log_path:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(report)
    return {"result": result, "error": error, "report": report}
'''


def _make_fixtures():
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    (SANDBOX / "docs").mkdir(parents=True)
    (SANDBOX / "utils").mkdir(parents=True)
    (SANDBOX / "docs" / "api.md").write_text(API_MD, encoding="utf-8")
    (SANDBOX / "utils" / "calculator.py").write_text(CALC_PY, encoding="utf-8")


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ping": "pong", "eval": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


SCENARIOS = [
    {
        "id": "S1", "ladder": "①trivial", "timeout": 240,
        "prompt": "E:/xihe-agent/src/core/agent.py 这个文件有多少行代码？只回答行数和一句说明。",
        "expect": "主直做（read_file/terminal 均可）",
        "judge": lambda c: "pass" if not (c & _DELEGATION_TOOLS) else "fail",
    },
    {
        "id": "S2", "ladder": "①trivial", "timeout": 240, "http": True,
        "prompt": "用 http 工具请求 {ping_url} ，告诉我返回的 JSON 内容。",
        "expect": "主直做（http 工具）",
        "judge": lambda c: "pass" if not (c & _DELEGATION_TOOLS) else "fail",
    },
    {
        "id": "S9", "ladder": "①trivial", "timeout": 240,
        "prompt": "帮我把这几件事记成任务清单：1. 检查邮件 2. 更新周报 3. 预约会议室",
        "expect": "主直做（todo 工具）",
        "judge": lambda c: "pass" if not (c & _DELEGATION_TOOLS) else "fail",
    },
    {
        "id": "S3", "ladder": "②专家域", "timeout": 420,
        "prompt": "帮我查一下工单 INC999999123 的处理进度。",
        "expect": "run_itsm_agent",
        "judge": lambda c: "pass" if "run_itsm_agent" in c else "fail",
    },
    {
        "id": "S4", "ladder": "②专家域", "timeout": 420,
        "prompt": "统计最近一个月 P2 及以上优先级工单的根因分布，给我一个汇总表格。",
        "expect": "run_itsm_agent",
        "judge": lambda c: "pass" if "run_itsm_agent" in c else "fail",
    },
    {
        "id": "S6", "ladder": "③多步委派", "timeout": 480,
        "prompt": f"重构 {_SANDBOX_POSIX}/utils 下的 calculator 模块："
                  "把过长的函数按职责拆分，并补充单元测试。",
        "expect": "delegate_task（主直做=partial，已知残留）",
        "judge": lambda c: ("pass" if "delegate_task" in c
                            else ("partial" if (c & {"write_file", "terminal", "patch"})
                                  else "fail")),
    },
    {
        "id": "S7", "ladder": "③多步委派", "timeout": 480,
        "prompt": f"把 {_SANDBOX_POSIX}/docs/api.md 全文翻译成英文，"
                  "保存到同目录 api_en.md。",
        "expect": "delegate_task，或 ≤4 次调用的精简直做（couple-of-calls 例外）",
        "judge": lambda c, n: ("pass" if ("delegate_task" in c or n <= 4)
                               else ("partial" if (c & {"write_file", "terminal", "patch"})
                                     else "fail")),
    },
    {
        "id": "S5", "ladder": "②专家域", "timeout": 600,
        "prompt": "用 claude 帮我审查下面这段代码的质量并给出改进建议：\n"
                  "```python\ndef add(a, b):\n    return a + b\n```\n"
                  "只需要审查这个函数本身。",
        "expect": "run_claude_agent",
        "judge": lambda c: "pass" if "run_claude_agent" in c else "fail",
    },
    {
        "id": "S8", "ladder": "④陷阱", "timeout": 600,
        "prompt": f"让外部引擎把 {_SANDBOX_POSIX}/docs/api.md 总结成 5 条要点。",
        "expect": "run_claude_agent（裸 external_agent=fail）",
        "judge": lambda c: ("pass" if "run_claude_agent" in c
                            else ("fail" if "external_agent" in c else "fail")),
    },
    {
        "id": "S10", "ladder": "④点名观察", "timeout": 600,
        "prompt": f"用 external_agent 工具把 {_SANDBOX_POSIX}/docs/api.md "
                  "总结成 3 条要点。",
        "expect": "观察：遵从点名(external_agent)或走专家均可",
        "judge": lambda c: "observed",
    },
]

RESULTS: list = []


@pytest.fixture(scope="module")
def core():
    import core.session as session_mod

    EVAL_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(EVAL_DIR / "eval.log"), filemode="w", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from core.config import load_config
    config = load_config()

    _make_fixtures()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ping_url = f"http://127.0.0.1:{server.server_address[1]}/ping"

    EVAL_DB.unlink(missing_ok=True)
    real_db_path = session_mod._DB_PATH
    session_mod._DB_PATH = EVAL_DB

    from core.agent import XiheAgent
    from core.auxiliary_client import AuxiliaryClient
    from core.compressor import ContextCompressor
    from core.session import SessionDB
    from core.toolsets import resolve_roster
    from core.store import merge_mounts
    from tools.mcp_tool import discover_mcp_tools

    discover_mcp_tools()
    db = SessionDB(config=config)
    aux = AuxiliaryClient(base_url=config["base_url"], api_key=config["api_key"],
                          model=config["model"], config=config)
    compressor = ContextCompressor(
        context_length=XiheAgent._get_context_length_static(config, config["model"]),
        threshold_percent=config["compression_threshold"], aux=aux)
    main_toolsets, main_skills = merge_mounts(
        "main", *resolve_roster(config, where="config.yaml"))

    def create_agent(cwd=None):
        return XiheAgent(config, shared_db=db, shared_aux=aux,
                         shared_compressor=compressor,
                         enabled_toolsets=main_toolsets,
                         skills_allowed=main_skills, cwd=cwd)

    ns = type("Core", (), {})()
    ns.db, ns.create_agent, ns.ping_url, ns.model = db, create_agent, ping_url, \
        config.get("model")
    yield ns

    session_mod._DB_PATH = real_db_path
    server.shutdown()
    if len(RESULTS) == len(SCENARIOS):
        write_reports([r for _, r in RESULTS], ns.model)


def run_scenario(db, create_agent, sc, ping_url):
    from core.session import SessionSource

    chat_id = f"eval-{sc['id'].lower()}"
    source = SessionSource(platform="eval", chat_id=chat_id,
                           user_id="eval", chat_type="dm")
    prompt = sc["prompt"].replace("{ping_url}", ping_url)

    agent = create_agent(cwd=str(SANDBOX))
    calls: list[tuple[str, str]] = []
    watchdog = threading.Timer(sc["timeout"], agent.interrupt)
    t0 = time.monotonic()
    final, err, exit_reason, usage = "", None, None, {}
    try:
        watchdog.start()
        final = agent.chat(
            source, prompt,
            tool_call_start_callback=lambda n, a: calls.append((n, a)),
        )
        exit_reason = getattr(agent, "_last_exit_reason", None)
        usage = dict(getattr(agent, "_turn_usage", {}))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        watchdog.cancel()

    elapsed = round(time.monotonic() - t0, 1)
    names = [n for n, _ in calls]
    called = set(names)
    try:
        verdict = sc["judge"](called, len(names)) if not err else "error"
    except TypeError:
        verdict = sc["judge"](called) if not err else "error"
    if exit_reason == "interrupted":
        verdict = f"timeout({verdict})"
    return {
        "id": sc["id"], "ladder": sc["ladder"], "prompt": prompt,
        "expect": sc["expect"], "verdict": verdict, "error": err,
        "exit_reason": exit_reason, "elapsed_s": elapsed,
        "tool_calls": names,
        "usage": usage,
        "final_text": (final or "")[:800],
    }


def _bucket(v) -> str:
    v = str(v)
    if v.startswith("timeout"):
        return "timeout"
    return v if v in ("pass", "partial", "fail", "error", "observed") else "fail"


def write_reports(results, model):
    counts = {}
    for r in results:
        counts[_bucket(r["verdict"])] = counts.get(_bucket(r["verdict"]), 0) + 1
    n_pass = counts.get("pass", 0)
    n_partial = counts.get("partial", 0)
    n_fail = counts.get("fail", 0) + counts.get("error", 0)
    n_timeout = counts.get("timeout", 0)

    REPORT_JSON.write_text(
        json.dumps({"model": model, "results": results}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    lines = [
        "# 路由评估报告", "",
        f"- 模型: `{model}`",
        f"- 判分: **{n_pass} pass / {n_partial} partial / {n_fail} fail**"
        f"（超时 {n_timeout}，观察项不计）", "",
        "| # | 阶梯 | 预期 | 实际工具序列 | 判定 | 耗时 | tokens(↑/↓) |",
        "|---|------|------|--------------|------|------|--------------|",
    ]
    for r in results:
        u = r.get("usage") or {}
        pt, ct = u.get("prompt", 0), u.get("completion", 0)
        lines.append(
            f"| {r['id']} | {r['ladder']} | {r['expect']} | "
            f"{' → '.join(r['tool_calls']) or '(无工具)'} | **{r['verdict']}** "
            f"| {r['elapsed_s']}s | {pt}/{ct} |")
    lines += ["", "## 各场景最终回复（截断）", ""]
    for r in results:
        lines += [f"### {r['id']} — {r['verdict']}", "",
                  f"> prompt: {r['prompt']}", "",
                  r["final_text"] or "(空)", ""]
        if r["error"]:
            lines += [f"error: {r['error']}", ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


class TestRoutingLadder:
    @pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s["id"])
    def test_scenario(self, core, sc):
        r = run_scenario(core.db, core.create_agent, sc, core.ping_url)
        RESULTS.append((sc, r))
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
