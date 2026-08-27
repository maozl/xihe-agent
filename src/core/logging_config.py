"""Logging configuration — console + optional file, with customizable format."""

import logging
import sys
from pathlib import Path

from core.config import AGENT_HOME

CONSOLE_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
FILE_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level=logging.WARNING, also_file=False,
                  log_file: str | Path = None, file_level=None):
    """Configure logging — always works regardless of call order.

    Args:
        level: Console (and default) log level.
        also_file: Also write to a log file.
        log_file: Custom log file path. Defaults to ~/.xihe-agent/agent.log.
        file_level: File-handler level (defaults to ``level``). Set lower than
            ``level`` (e.g. INFO with level=WARNING) to capture detail in the
            file while keeping the console quiet — root is lowered to the more
            verbose of the two so records still reach the handlers.
    """
    file_level = file_level or level
    root = logging.getLogger()
    root.setLevel(min(level, file_level))  # don't drop records before handlers
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    if also_file:
        path = Path(log_file) if log_file else AGENT_HOME / "agent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotating, not bare: the gateway/serve log every tool call at INFO
        # and run for weeks — unbounded agent.log growth once ate the disk.
        # Not multi-process safe (two xihe instances sharing one AGENT_HOME
        # may interleave during rollover); the worst case is lost log lines,
        # which beats silent growth. delay=True avoids empty log files on
        # short runs.
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(path, encoding="utf-8",
                                 maxBytes=10 * 1024 * 1024, backupCount=3,
                                 delay=True)
        fh.setLevel(file_level)
        fh.setFormatter(logging.Formatter(FILE_FORMAT))
        root.addHandler(fh)

    # Silence chatty third-party loggers that flood the log at INFO:
    # - httpx/httpcore: logs EVERY HTTP request (e.g. MCP streamable-http poll
    #   every 30s) — noise, real errors still surface at WARNING+.
    # - mcp.client.*: stream disconnect/reconnect INFO spam.
    # - anyio/openai/urllib3: similar per-request/per-loop INFO noise.
    for noisy in ("httpx", "httpcore", "mcp", "anyio", "openai", "urllib3",
                  "asyncio", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
