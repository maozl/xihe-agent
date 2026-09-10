#!/usr/bin/env python3
"""Xihe Agent — unified CLI entry point.

Usage:
    xihe                            # Interactive chat (default)
    xihe chat -q "hello"            # Single query
    xihe chat -s my-project         # Named session
    xihe gateway                    # Run messaging gateway
    xihe gateway --platform wecom   # Gateway with platform override
    xihe serve                      # Run as an HTTP+WS service (for the desktop / external clients)
    xihe serve --port 7788          # Serve on a custom port
    xihe --config ~/xihe-instance.yaml gateway  # Run with a specific instance config
    xihe cron list                  # List cron jobs
    xihe cron create 30m "提醒我"   # Create cron job
    xihe doctor                     # Environment health check
    xihe doctor gateway             # Also check platform credentials
    xihe --version
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import load_config, seed_default_config
from core.logging_config import setup_logging
from core.version import VERSION


def cmd_chat(args):
    from cli.chat import run_chat
    return run_chat(args)


def cmd_gateway(args):
    from gateway import run_gateway
    return run_gateway(args)


def cmd_cron(args):
    import json
    from tools import registry
    import tools.cronjob_tools  # noqa: F401

    action = getattr(args, "cron_action", "list")

    if action == "list":
        from core.services.scheduler import _load_jobs, _jobs
        _load_jobs()
        if not _jobs:
            print("No cron jobs.")
            return 0
        for job_id, job in _jobs.items():
            status = "PAUSED" if not job.get("enabled", True) else "ACTIVE"
            print(f"  [{status}] {job_id}  {job.get('name', '')}  "
                  f"({job.get('schedule_display', '')})  "
                  f"deliver={job.get('deliver', 'origin')}  "
                  f"next={job.get('next_run_at') or '-'}")
        return 0

    if action in ("create", "add"):
        schedule = getattr(args, "schedule", "")
        prompt = getattr(args, "prompt", "")
        if not schedule or not prompt:
            print("Error: schedule and prompt are required", file=sys.stderr)
            return 1
        result = json.loads(registry.dispatch("cronjob", json.dumps({
            "action": "create", "name": getattr(args, "name", ""),
            "schedule": schedule, "prompt": prompt,
            "deliver": getattr(args, "deliver", "origin"),
        }), context={"chat_id": "cli", "platform": "cli"}))
        if result.get("success"):
            print(f"Created: {result['job_id']}  schedule={result.get('schedule', '')}  "
                  f"next={result.get('next_run_at', '-')}")
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
        return 0

    if action in ("remove", "delete", "rm"):
        job_id = getattr(args, "job_id", "")
        result = json.loads(registry.dispatch("cronjob", json.dumps({
            "action": "delete", "job_id": job_id,
        })))
        if result.get("success"):
            print(f"Removed: {job_id}")
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
        return 0

    if action == "run":
        job_id = getattr(args, "job_id", "")
        result = json.loads(registry.dispatch("cronjob", json.dumps({
            "action": "run", "job_id": job_id,
        })))
        if result.get("success"):
            print(f"Triggered: {job_id}")
        else:
            print(f"Error: {result.get('error', 'unknown')}", file=sys.stderr)
            return 1
        return 0

    print(f"Unknown action: {action}. Use: list, create, remove, run", file=sys.stderr)
    return 1


def cmd_doctor(args):
    from cli.doctor import run_doctor
    return run_doctor(args)


def cmd_serve(args):
    """Run xihe as an HTTP+WebSocket service.

    Same agent core as chat/gateway, fronted by aiohttp so the desktop (or any
    external client) can drive it over a neutral protocol. See gateway/serve/.
    """
    from gateway.serve import run_serve
    # Seed BEFORE load: on first start the template (with its toolsets roster)
    # must exist when load_config reads, else serve comes up with zero tools
    # and built-in defaults until the next restart.
    seed_default_config(getattr(args, "config", None))
    config = load_config(getattr(args, "config", None))
    setup_logging(level=logging.INFO, also_file=True)
    run_serve(config, host=args.host, port=args.port, version=VERSION)
    return 0


def _add_config_arg(parser, suppress=False):
    """Register --config on a parser. The top-level call uses default=None so
    ``args.config`` always exists; subparsers pass suppress=True (SUPPRESS) so a
    value parsed before the subcommand (``xihe --config x.yaml chat``) isn't
    clobbered by the subparser's own default, while ``xihe chat --config x.yaml``
    still works too."""
    parser.add_argument(
        "--config", metavar="PATH",
        default=argparse.SUPPRESS if suppress else None,
        help="Instance config YAML: sets agent_home (→ data root) and overrides "
             "model/platform/credentials — run multiple xihes side by side.",
    )


def main():
    parser = argparse.ArgumentParser(
        prog="xihe",
        description="Xihe Agent — AI assistant with tool-calling capabilities",
    )
    parser.add_argument("--version", "-V", action="store_true", help="Show version")
    _add_config_arg(parser)

    sub = parser.add_subparsers(dest="command", help="Available commands")

    p_chat = sub.add_parser("chat", help="Interactive chat (default)")
    _add_config_arg(p_chat, suppress=True)
    p_chat.add_argument("-q", "--query", help="Single query (non-interactive)")
    p_chat.add_argument("-s", "--session", default=None, help="Session key")
    p_chat.add_argument("-r", "--resume", action="store_true",
                        help="List recent CLI sessions and resume one (interactive)")
    p_chat.set_defaults(func=cmd_chat)

    p_gw = sub.add_parser("gateway", help="Run messaging gateway")
    _add_config_arg(p_gw, suppress=True)
    p_gw.add_argument("--platform", default=None, help="Platform (default: from config)")
    p_gw.set_defaults(func=cmd_gateway)

    p_cron = sub.add_parser("cron", help="Manage scheduled tasks")
    _add_config_arg(p_cron, suppress=True)
    cron_sub = p_cron.add_subparsers(dest="cron_action")
    cron_sub.add_parser("list", help="List jobs")
    p_create = cron_sub.add_parser("create", aliases=["add"], help="Create a job")
    p_create.add_argument("schedule", help="Schedule (30m, every 2h, 0 9 * * *)")
    p_create.add_argument("prompt", help="Prompt to execute")
    p_create.add_argument("--name", default="", help="Job name")
    p_create.add_argument("--deliver", default="origin", help="origin, local, or platform:chat_id")
    p_rm = cron_sub.add_parser("remove", aliases=["delete", "rm"], help="Remove a job")
    p_rm.add_argument("job_id", help="Job ID")
    p_run = cron_sub.add_parser("run", help="Trigger a job immediately")
    p_run.add_argument("job_id", help="Job ID")
    p_cron.set_defaults(func=cmd_cron)

    p_serve = sub.add_parser(
        "serve", help="Run as an HTTP+WS service (for the desktop / external clients)")
    _add_config_arg(p_serve, suppress=True)
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=7788, help="Bind port (default 7788)")
    p_serve.set_defaults(func=cmd_serve)

    p_doc = sub.add_parser("doctor", help="Environment health check")
    _add_config_arg(p_doc, suppress=True)
    p_doc.add_argument("doctor_mode", nargs="?", default=None,
                       choices=["chat", "gateway"],
                       help="Check perspective (default: chat; gateway also checks platform credentials)")
    p_doc.add_argument("--no-net", action="store_true",
                       help="Skip the model-endpoint connectivity test")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()

    if args.version:
        print(f"xihe-agent {VERSION}")
        return 0

    if not hasattr(args, "func"):
        args.command = "chat"
        args.func = cmd_chat
        args.query = None
        args.session = None
        args.resume = False

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
