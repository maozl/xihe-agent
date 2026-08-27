"""Readiness diagnostics — one probe set, three consumers.

`xihe doctor` (CLI), `GET /readiness` (serve / desktop), and the gateway
startup preflight all ask the same questions: is the model connection
configured, which optional capabilities actually load, and what should the
user fix next. Every probe lives here so the answers cannot drift apart.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

# (importable module name, what breaks without it)
OPTIONAL_DEPS = [
    ("playwright", "浏览器工具 (browser_*)"),
    ("paddleocr", "离线 OCR (image_ocr)，另需 paddlepaddle"),
    ("RestrictedPython", "计算沙箱 (run_sandbox_code)"),
    ("mcp", "MCP 客户端 (mcp_servers)"),
    ("paramiko", "SSH 工具 (ssh_*)"),
]

# Required platform credentials, mirroring each adapter's start() check —
# the gateway preflight and doctor turn "two log lines + exit 1" into a
# named-field fix list.
PLATFORM_REQUIRED_FIELDS = {
    "wecom": ("bot_id", "secret"),
    "feishu": ("app_id", "app_secret"),
}


def platform_missing_fields(platform: str, platform_config: dict) -> list[str]:
    return [f for f in PLATFORM_REQUIRED_FIELDS.get(platform, ())
            if not str((platform_config or {}).get(f, "") or "").strip()]


def platform_config_missing_message(platform: str, missing: list[str]) -> str:
    sample = "\n".join(f'      {f}: "..."' for f in missing)
    return (
        f"Error: platforms.{platform} 缺少必填字段: {', '.join(missing)}\n"
        "\n"
        f"在 config.yaml 里补上：\n"
        "\n"
        f"  platforms:\n    {platform}:\n{sample}\n"
        "\n"
        "字段说明见 config.example.yaml。"
    )


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def probe_optional_deps() -> list[dict]:
    """Import-probe each optional dependency (cheap, no side effects)."""
    return [{"module": m, "purpose": p, "ok": _module_available(m)}
            for m, p in OPTIONAL_DEPS]


def probe_system_browser() -> str | None:
    """Find a system Chrome/Edge — browser tools drive it over CDP.

    The deployment is airgapped: bundled Chromium can't be downloaded, so
    a missing system browser means browser_* tools are effectively dead
    even with Playwright installed.
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        for env, sub in (
            ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
            ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
            ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
            ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
            ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
        ):
            base = __import__("os").environ.get(env)
            if base:
                candidates.append(Path(base) / sub)
    else:
        candidates += [
            Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"), Path("/usr/bin/microsoft-edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    for c in candidates:
        if c.is_file():
            return str(c)
    for exe in ("google-chrome", "chrome", "msedge", "chromium", "chromium-browser"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def check_connectivity(base_url: str, api_key: str, timeout: float = 8.0) -> dict:
    """GET {base_url}/models — reachable + auth accepted, plus model list.

    404 counts as reachable (some internal gateways don't expose /models);
    401/403 means the key is bad, which is the answer users actually need.
    """
    import httpx
    url = f"{str(base_url or '').rstrip('/')}/models"
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except Exception as e:
        return {"ok": False, "status_code": None, "models": [], "error": str(e)}
    out = {"ok": resp.status_code < 400, "status_code": resp.status_code,
           "models": [], "error": None}
    if resp.status_code == 200:
        try:
            data = resp.json().get("data", [])
            out["models"] = sorted({m.get("id") for m in data
                                    if isinstance(m, dict) and m.get("id")})
        except Exception:
            pass
    elif resp.status_code in (401, 403):
        out["error"] = f"HTTP {resp.status_code} — api_key 无效或无权限"
    elif resp.status_code == 404:
        out["error"] = "端点可达，但不提供 GET /models（不影响使用）"
    else:
        out["error"] = f"HTTP {resp.status_code}"
    return out


def _web_search_key_present(config: dict) -> bool:
    # config.example.yaml nests keys under web.<provider>.api_key; accept flat too
    web_cfg = config.get("web") or {}
    if isinstance(web_cfg, dict):
        for provider in ("tavily", "serpapi", "bing", "firecrawl"):
            entry = web_cfg.get(provider)
            key = entry.get("api_key") if isinstance(entry, dict) else entry
            if key:
                return True
    return False


def _vision_model_present(config: dict) -> bool:
    if config.get("vision_model"):
        return True
    aux = config.get("auxiliary") or {}
    vision = aux.get("vision") or {} if isinstance(aux, dict) else {}
    return bool(vision.get("model"))


def main_agent_names(config: dict):
    """Agent-visible tool names for the main roster, resolved exactly like
    SharedContext does (roster + store mounts + base floor). Returns
    (names, toolsets, warnings)."""
    from tools import registry
    from core.toolsets import resolve_roster
    from core.store import merge_mounts
    warnings: list[str] = []
    toolsets, skills = resolve_roster(config, where="config.yaml",
                                      warnings=warnings)
    toolsets, skills = merge_mounts("main", toolsets, skills)
    # resolve_roster's bare "no tools" line gets replaced with an actionable one
    warnings[:] = [w for w in warnings if "no tools" not in w]
    if toolsets == []:
        warnings.append("toolsets 未配置 — 主 agent 无任何工具（纯对话）。"
                        "config.example.yaml 里有推荐清单。")
    eff = None
    if toolsets is not None:
        eff = set(toolsets) | {"base"} if toolsets else set()
    schemas = registry.get_schemas(toolsets=eff)
    names = {s["function"]["name"] for s in schemas}
    return names, toolsets, warnings


def capability_matrix(config: dict, toolsets, names: set) -> dict:
    """{capability: {ready, reason}} — reason carries the fix when off."""
    def cap(ready, reason=None):
        return {"ready": bool(ready), "reason": reason}

    def roster_lacks(group):
        return toolsets is not None and group not in toolsets

    browser_on = any(n.startswith("browser_") for n in names)
    if browser_on:
        browser = cap(True)
    elif not _module_available("playwright"):
        browser = cap(False, "未安装 playwright：pip install playwright（浏览器走系统 Chrome/Edge）")
    elif roster_lacks("web"):
        browser = cap(False, "toolsets 未包含 web")
    else:
        browser = cap(False, "browser 工具未注册（检查 agent.log）")

    if "vision_analyze" in names:
        vision = cap(True)
    elif not _vision_model_present(config):
        vision = cap(False, "vision_model 未配置（主模型通常非多模态）— 配 config.yaml 的 vision_model")
    elif roster_lacks("media"):
        vision = cap(False, "toolsets 未包含 media")
    else:
        vision = cap(False, "vision 工具未注册")

    if "image_ocr" in names:
        ocr = cap(True)
    elif not _module_available("paddleocr"):
        ocr = cap(False, "未安装 paddleocr / paddlepaddle（离线 OCR）")
    elif roster_lacks("media"):
        ocr = cap(False, "toolsets 未包含 media")
    else:
        ocr = cap(False, "OCR 工具未注册")

    if "web_search" in names:
        search = cap(True)
    elif not _web_search_key_present(config):
        search = cap(False, "web 段未配任何搜索 key（tavily/serpapi/bing/firecrawl）")
    elif roster_lacks("web"):
        search = cap(False, "toolsets 未包含 web")
    else:
        search = cap(False, "web_search 工具未注册")

    if "run_sandbox_code" in names:
        sandbox = cap(True)
    elif not _module_available("RestrictedPython"):
        sandbox = cap(False, "未安装 RestrictedPython（计算沙箱）")
    else:
        sandbox = cap(False, "run_sandbox_code 未注册")

    return {"browser": browser, "vision": vision, "ocr": ocr,
            "web_search": search, "sandbox": sandbox}


def readiness_report(config: dict, *, mode: str = "chat") -> dict:
    """Aggregate readiness — JSON-safe (no credential values escape)."""
    names, toolsets, warnings = main_agent_names(config)
    missing = []
    if not config.get("api_key"):
        missing.append({"item": "api_key", "action": "在 config.yaml 填入模型 api_key"})
    platform = None
    if mode == "gateway":
        pname = config.get("platform", "wecom")
        pcfg = (config.get("platforms") or {}).get(pname, {})
        miss = platform_missing_fields(pname, pcfg)
        platform = {"name": pname, "missing_fields": miss}
        missing += [{"item": f"platforms.{pname}.{f}",
                     "action": "在 config.yaml 补上该字段（示例见 config.example.yaml）"}
                    for f in miss]
    mcp_servers = []
    try:
        from tools.mcp_tool import get_mcp_status
        mcp_servers = get_mcp_status()
    except Exception:
        pass
    return {
        "ok": not missing,
        "mode": mode,
        "model": config.get("model"),
        "base_url": bool(config.get("base_url")),
        "api_key_set": bool(config.get("api_key")),
        "platform": platform,
        "tools": {"count": len(names)},
        "capabilities": capability_matrix(config, toolsets, names),
        "mcp_servers": mcp_servers,
        "warnings": warnings,
        "missing": missing,
    }
