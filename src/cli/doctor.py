"""`xihe doctor [chat|gateway]` — one-command environment health check.

Renders core.diagnostics probes as an actionable checklist: every failing
line names the fix (install command, config field) instead of just a state.
Exit 0 = no blocking problems; 1 = something in ``missing`` needs a fix.
"""
import sys

from core.config import load_config, AGENT_HOME, seed_default_config
from core.diagnostics import (readiness_report, probe_optional_deps,
                              probe_system_browser, check_connectivity)


def _mark(ok, warn=False):
    return "[OK]" if ok else ("[--]" if warn else "[XX]")


def run_doctor(args) -> int:
    config = load_config(getattr(args, "config", None))
    seed_default_config(getattr(args, "config", None))
    mode = getattr(args, "doctor_mode", None) or "chat"
    no_net = bool(getattr(args, "no_net", False))
    # Show the file actually read — an instance config (--config x.yaml) is the
    # source, not AGENT_HOME/config.yaml.
    cfg_path = getattr(args, "config", None) or (AGENT_HOME / "config.yaml")
    print(f"xihe doctor (mode={mode})  config: {cfg_path}")

    # Registry must be loaded for capability probes (this also connects MCP
    # servers — a dead server blocks here up to its timeout; that itself is
    # diagnostic information the MCP section reports).
    from tools import load_all_tools
    load_all_tools()

    report = readiness_report(config, mode=mode)
    problems = 0

    print("\n[配置]")
    api_ok = report["api_key_set"]
    print(f"  {_mark(api_ok)} api_key {'已配置' if api_ok else '未配置 → 编辑 config.yaml 填入'}")
    if not api_ok:
        problems += 1
    print(f"  {_mark(True)} model: {report['model']}")
    if report.get("platform"):
        p = report["platform"]
        p_ok = not p["missing_fields"]
        print(f"  {_mark(p_ok)} platforms.{p['name']}"
              + ("" if p_ok else f" 缺少: {', '.join(p['missing_fields'])}"))
        if not p_ok:
            problems += 1

    print("\n[依赖]")
    for dep in probe_optional_deps():
        print(f"  {_mark(dep['ok'], warn=not dep['ok'])} {dep['module']}"
              f"{'' if dep['ok'] else ' 未安装'} — {dep['purpose']}")

    print("\n[浏览器]")
    browser = probe_system_browser()
    print(f"  {_mark(bool(browser), warn=not browser)}"
          f" 系统浏览器: {browser or '未找到 Chrome/Edge → browser 工具不可用（走系统浏览器，不下载 Chromium）'}")

    print(f"\n[工具] 主 agent 可见 {report['tools']['count']} 个")
    print("\n[能力]")
    caps = report["capabilities"]
    labels = {"browser": "浏览器", "vision": "视觉分析", "ocr": "离线 OCR",
              "web_search": "网络搜索", "sandbox": "计算沙箱"}
    for key, label in labels.items():
        c = caps[key]
        line = f"  {_mark(c['ready'], warn=not c['ready'])} {label}: {'可用' if c['ready'] else '不可用'}"
        if c["reason"]:
            line += f" — {c['reason']}"
        print(line)

    mcp = report.get("mcp_servers") or []
    print("\n[MCP]")
    if not mcp:
        print("  [--] 未配置 MCP 服务器")
    for s in mcp:
        ok = s.get("connected")
        print(f"  {_mark(ok, warn=not ok)} {s.get('name')} ({s.get('transport')})"
              f"{'' if ok else ' 连接失败'}"
              + (f" — {s.get('tools')} 工具" if ok else ""))

    for w in report["warnings"]:
        print(f"\n[注意] {w}")

    if no_net:
        print("\n[连通性] 跳过（--no-net）")
    else:
        print("\n[连通性]")
        net = check_connectivity(config.get("base_url"), config.get("api_key"))
        if net["ok"]:
            tail = (f" — 发现 {len(net['models'])} 个模型"
                    + (f": {', '.join(net['models'][:8])}" if net["models"] else ""))
            print(f"  {_mark(True)} {config.get('base_url')}{tail}")
            if net.get("error"):
                print(f"      ({net['error']})")
        else:
            print(f"  {_mark(False)} {config.get('base_url')} — {net['error']}")
            problems += 1

    if problems:
        print(f"\n结论: {problems} 项需处理后可用。")
        return 1
    print("\n结论: 无阻塞问题。")
    return 0


def main():  # `python -m cli.doctor` convenience
    class _A:
        config = None
        doctor_mode = None
        no_net = False
    sys.exit(run_doctor(_A()) or 0)
