"""SharedContext — the mode-agnostic application assembly all three run modes
(chat / gateway / serve) build on: one SQLite connection, one auxiliary LLM
client, one compressor, one shared main-model client, one tool registry load,
and the main-agent roster from config.

Lives in core (not app/) because the modes construct it themselves inside
run_chat / run_gateway / run_serve — it must sit BELOW the mode layer, never
inside the launcher."""

from pathlib import Path


class SharedContext:
    """Heavy state shared across per-message XiheAgent instances in gateway mode.

    Creating a new XiheAgent per message is cheap — the expensive objects
    (SQLite connection, auxiliary LLM client, context compressor) are reused.
    """

    def __init__(self, config: dict):
        from core.agent import XiheAgent
        from core.agent.auxiliary_client import AuxiliaryClient
        from core.agent.compressor import ContextCompressor
        from core.session import SessionDB

        self.config = config
        self.db = SessionDB(config=config)
        self.aux = AuxiliaryClient(
            base_url=config["base_url"],
            api_key=config["api_key"],
            model=config["model"],
            config=config,
        )
        context_length = XiheAgent._get_context_length_static(config, config["model"])
        self.compressor = ContextCompressor(
            context_length=context_length,
            threshold_percent=config["compression_threshold"],
            aux=self.aux,
        )
        # Shared main-model client: per-message agents otherwise each pay a
        # fresh httpx pool (TCP+TLS handshake) on their first call. Built only
        # when a key is configured — an empty key must keep failing at agent
        # creation (serve translates that to its onboarding error), not crash
        # SharedContext startup. httpx.Client is thread-safe, so one instance
        # serves all concurrent turns.
        self.client = None
        if config.get("api_key"):
            import httpx
            from openai import OpenAI
            self.client = OpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                timeout=httpx.Timeout(120.0, connect=10.0),
            )

        from tools import (session_search_tool, vision_tools,
                           image_generation_tool, tts_tool)
        session_search_tool.set_session_db(self.db)
        vision_tools.set_auxiliary(self.aux)
        image_generation_tool.set_auxiliary(self.aux)
        tts_tool.set_auxiliary(self.aux)


        # Main-agent roster from config.yaml top-level keys (toolsets/skills),
        # resolved by the same resolve_roster specialists use. Absent → no
        # tools (warning). Cron/command agents keep the full set. Store mounts
        # union in here, so a desktop mount takes effect on the next start.
        from core.toolsets import resolve_roster
        from core.services.store import merge_mounts
        self.main_toolsets, self.main_skills = merge_mounts(
            "main", *resolve_roster(config, where="config.yaml"))

    def create_agent(self, enabled_toolsets=None, cwd=None,
                     skills_allowed=None) -> "XiheAgent":
        """Create a fresh XiheAgent sharing this context.

        No args → unrestricted agent (cron jobs, slash-command context); the
        main chat agent passes main_toolsets/main_skills explicitly.
        """
        from core.agent import XiheAgent
        return XiheAgent(
            self.config,
            shared_db=self.db,
            shared_aux=self.aux,
            shared_compressor=self.compressor,
            client=self.client,
            enabled_toolsets=enabled_toolsets,
            skills_allowed=skills_allowed,
            cwd=cwd,
        )


def bootstrap_process(ctx: "SharedContext", adapter=None) -> None:
    """Process-level composition root — every mode entrypoint calls this
    once, explicitly (SharedContext construction is side-effect-free, so
    tests can build one without spawning threads).

    Order: static tool registry → background MCP discovery → specialist
    dispatch tools → cron (agent factory + scheduler). The scheduler's file
    lock keeps a gateway and a serve on one agent_home from double-firing
    jobs; the loser just lists. All imports function-level: none of this may
    fire at core import time (before the api_key gate has its say).
    """
    from tools import load_all_tools, start_mcp_discovery
    load_all_tools()
    start_mcp_discovery()
    from tools.specialist_agent_tool import sync_specialist_agent_tools
    sync_specialist_agent_tools()

    from core.services.scheduler import (
        set_agent_factory, set_platform_adapter, start_scheduler,
    )
    set_agent_factory(ctx.create_agent)
    start_scheduler()
    if adapter is not None:
        from tools import send_message_tool
        send_message_tool.set_adapter(adapter)
        set_platform_adapter(adapter)


def init_agent(config: dict, platform_adapter=None, cwd=None):
    """Create XiheAgent and wire all tool dependencies.

    For CLI mode: returns a single long-lived agent (main-agent roster from
    config). ``cwd`` defaults to the launch directory.
    For gateway/serve mode: use SharedContext.create_agent() instead (no cwd).
    """
    ctx = SharedContext(config)
    bootstrap_process(ctx, adapter=platform_adapter)
    agent = ctx.create_agent(enabled_toolsets=ctx.main_toolsets,
                             skills_allowed=ctx.main_skills,
                             cwd=cwd or Path.cwd())
    return agent
