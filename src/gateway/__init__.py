"""Gateway layer — messaging bot (bot.py) + desktop service (serve/) +
platform adapters (platforms/). Modes share the agent core; the launcher
that dispatches into them lives in app/."""

from gateway.bot import run_gateway
