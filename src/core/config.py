"""Configuration loading.

Single-source model: ALL configuration lives in one config.yaml file — the one
named by ``--config`` (peeked from ``sys.argv`` at import time, or passed
explicitly), defaulting to ``~/.xihe-agent/config.yaml``. There are no
environment-variable overrides for config *values* and no ``.env`` file: the
YAML is the single source of truth. The launch-time ``--config`` / ``AGENT_HOME``
locators still come from the CLI / shell, since they decide *which* YAML to
read rather than being values inside one.
"""

import copy
import os
import sys
import threading
from pathlib import Path

def _find_repo_root() -> Path:
    """Repo root = the directory holding ``pyproject.toml``.

    Walked up from this file so it survives relocating the package (e.g.
    moving it under ``src/``). Relative ``agent_home`` paths in a ``--config``
    file resolve against the repo root regardless of where this module sits,
    so the anchor must not hardcode the module's depth.
    """
    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "pyproject.toml").is_file():
            return cand
    return here.parent  # last-resort fallback


_REPO_ROOT = _find_repo_root()


def _peek_config_flag() -> Path | None:
    """Find a ``--config <path>`` (or ``--config=<path>``) in ``sys.argv``.

    Launch-time instance selector — a *config source* on equal footing with the
    ``AGENT_HOME`` env var: both are set before any user code runs, so both are
    readable at import time, before argparse parses anything. Returns the
    resolved path (``~``-expanded, absolute) or ``None``. Skips ``--config``
    immediately followed by another ``-flag`` so it doesn't swallow the next
    option.
    """
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return Path(argv[i + 1]).expanduser().resolve()
        if a.startswith("--config="):
            return Path(a[len("--config="):]).expanduser().resolve()
    return None


def _read_agent_home_from_file(yaml_path) -> str | None:
    """Read ``agent_home`` from a ``--config`` instance YAML file.

    The user config.yaml lives *inside* AGENT_HOME, so it cannot define
    AGENT_HOME (chicken-and-egg: finding it requires knowing AGENT_HOME first);
    but the instance ``--config`` YAML lives outside AGENT_HOME, so it can set
    the data root. ``${VAR}`` / ``$VAR`` in the value are shell-expanded so a
    portable instance file can say ``agent_home: ${HOME}/xihe/prod``.
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        return None
    try:
        import yaml
        with open(yaml_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            return None
        val = cfg.get("agent_home")
        if val and str(val).strip():
            return os.path.expandvars(str(val)).strip()
    except Exception:
        pass
    return None


def _resolve_agent_home() -> Path:
    """Resolve the user-data root.

    Priority (low → high): ``~/.xihe-agent`` < ``AGENT_HOME`` env var <
    ``--config <path>`` yaml's ``agent_home``. The ``--config`` flag fully
    describes an instance (data root + config overrides), so it wins. A
    relative ``agent_home`` there resolves against the repo root, so a
    ``--config`` file checked into a repo can say ``agent_home: .xihe-agent``
    and mean ``<repo>/.xihe-agent`` regardless of cwd; ``AGENT_HOME`` is
    expected absolute. ``--config`` is read at import time via
    ``_peek_config_flag``.

    Side effect: when ``--config`` is present, its path is exported to the
    ``XIHE_CONFIG_FILE`` env var so ``load_config()`` can pick it up without
    being threaded through every caller.
    """
    cli_cfg = _peek_config_flag()
    if cli_cfg:
        os.environ["XIHE_CONFIG_FILE"] = str(cli_cfg)
        cli_home = _read_agent_home_from_file(cli_cfg)
        if cli_home:
            p = Path(cli_home).expanduser()
            if not p.is_absolute():
                p = (_REPO_ROOT / p).resolve()
            return p

    env_home = os.environ.get("AGENT_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".xihe-agent"


AGENT_HOME = _resolve_agent_home()
AGENT_HOME.mkdir(parents=True, exist_ok=True)


def expand_agent_vars(text):
    """Expand path variables in skill/role text.

    ``${AGENT_HOME}`` resolves to AGENT_HOME — the actual data root, whether set
    via the AGENT_HOME env var or the ``--config`` file's ``agent_home``, or the
    default ~/.xihe-agent. Lets skills reference the data root without
    hardcoding ~/.xihe-agent, so they follow the configured root automatically.
    """
    if not text:
        return text
    return text.replace("${AGENT_HOME}", str(AGENT_HOME))


# (mtime_ns, size) → parsed config, per config path. Lock guards the dict;
# parses happen outside it.
_CONFIG_CACHE: dict[str, tuple[tuple[int, int] | None, dict]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()


def load_config(config_path: str | None = None) -> dict:
    """Load config from a single config.yaml source.

    All configuration lives in one YAML file — the one named by ``--config``
    (peeked from ``sys.argv`` at import time, or passed explicitly), defaulting
    to ``~/.xihe-agent/config.yaml``. There are no environment-variable
    overrides for config values and no ``.env`` file: the YAML is the single
    source of truth, and its values are read literally (no ``${VAR}`` expansion).

    ``config_path`` defaults to the ``--config`` path peeked from ``sys.argv``
    (stashed in ``XIHE_CONFIG_FILE`` by ``_resolve_agent_home``), so callers
    need not pass it explicitly; an explicit arg takes precedence.

    Results are cached per path keyed on (mtime_ns, size) and returned as
    deep copies — load_config sits on hot paths (tool check_fns call it
    several times per agent-loop iteration) and the YAML parse dominated.
    A missing file caches too; creating it changes the stamp and reloads.
    """
    path = str(config_path or os.environ.get("XIHE_CONFIG_FILE")
               or AGENT_HOME / "config.yaml")
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None

    with _CONFIG_CACHE_LOCK:
        hit = _CONFIG_CACHE.get(path)
    if hit is not None and hit[0] == stamp:
        return copy.deepcopy(hit[1])

    config = _parse_config_file(path)

    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[path] = (stamp, copy.deepcopy(config))
    return config


def clear_config_cache() -> None:
    """Drop cached configs (tests, forced reload)."""
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.clear()


def config_stamp(config_path: str | None = None) -> tuple[int, int] | None:
    """Cheap (mtime_ns, size) fingerprint of the config file, no parse —
    lets hot callers (registry.get_schemas) invalidate their caches without
    a read/parse round-trip."""
    path = str(config_path or os.environ.get("XIHE_CONFIG_FILE")
               or AGENT_HOME / "config.yaml")
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _parse_config_file(config_path: str) -> dict:
    """Uncached parse of one config.yaml (the load_config body proper)."""

    config = {
        "model": "glm-5.1-tc",
        "vision_model": "",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "platform": "wecom",
        "max_iterations": 30,
        "compression_threshold": 0.50,
        # 思考/回复语言引导：zh | en | auto（不引导）。桌面端可配置。
        "language": "zh",
        # GLM-class thinking models spend completion tokens on reasoning too;
        # 4096 let a long thinking phase starve content to empty.
        "max_completion_tokens": 8192,
    }

    # 2. Apply the single YAML source (literal values — no ${VAR} expansion,
    #    no environment-variable override).
    try:
        import yaml
        yaml_path = Path(config_path)
        if yaml_path.exists():
            with open(yaml_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if not isinstance(cfg, dict):
                cfg = {}
            for key in ("model", "vision_model", "base_url", "api_key", "platform",
                        "max_iterations", "compression_threshold",
                        "max_completion_tokens", "language",
                        "toolsets", "skills", "request_extra"):
                if key in cfg and cfg[key] is not None:
                    config[key] = cfg[key]
            # Nested sections (replace — single source, nothing to merge against)
            for section in ("models", "platforms", "session", "auxiliary",
                            "delegation", "external_agents",
                            "approvals", "kbs", "specialists", "store"):
                if section in cfg and isinstance(cfg[section], dict):
                    config[section] = cfg[section]
            if "mcp_servers" in cfg and isinstance(cfg["mcp_servers"], dict):
                config["mcp_servers"] = cfg["mcp_servers"]
    except ImportError:
        pass

    for section in ("models", "platforms", "session", "auxiliary",
                    "delegation", "external_agents",
                    "mcp_servers", "approvals", "kbs", "specialists", "store"):
        config.setdefault(section, {})
    return config


def seed_default_config(config_path: str | None = None) -> bool:
    """Create config.yaml from the annotated template when it doesn't exist.

    First-start convenience: the user gets a fully commented config (with a
    sensible toolsets roster) to edit instead of an error. Never touches an
    existing file; returns True only when one was created this call.
    """
    path = Path(config_path or os.environ.get("XIHE_CONFIG_FILE")
                or AGENT_HOME / "config.yaml")
    if path.exists():
        return False
    src = _REPO_ROOT / "config.example.yaml"
    if not src.is_file():
        return False
    try:
        header = ("# xihe 首次启动时按模板生成——填好 api_key（并按需调整 model/toolsets）后即可使用。\n")
        path.write_text(header + src.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError:
        return False


def api_key_missing_message(config_path: str | None = None,
                            seeded: bool = False) -> str:
    """Stderr message for the api_key gate (xihe chat / xihe gateway).

    ``seeded`` = config.yaml was just created from the template this start —
    then the message only points at the file. Otherwise it embeds a minimal
    working config so a first-run user can paste their way to a working
    setup without hunting for the template file.
    """
    path = (config_path or os.environ.get("XIHE_CONFIG_FILE")
            or str(AGENT_HOME / "config.yaml"))
    if seeded:
        return (
            "Error: api_key is not configured.\n"
            "\n"
            "A default config with full comments was just created:\n"
            f"  {path}\n"
            "Open it, fill in api_key, then run xihe again."
        )
    return (
        "Error: api_key is not configured.\n"
        "\n"
        f"xihe reads a single config file: {path}\n"
        "Set up an OpenAI-compatible model connection there, e.g.:\n"
        "\n"
        "  model: your-model\n"
        "  base_url: https://api.example.com/v1\n"
        "  api_key: sk-...\n"
        '  toolsets: ["files", "terminal", "http"]\n'
        "\n"
        "Every option is annotated in config.example.yaml in the repo."
    )
