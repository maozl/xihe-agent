"""Shared plumbing across the serve business modules: platform identity, WS
wire limits, and the plain-function helpers the stateless modules use."""

import asyncio

from aiohttp import web

# Platform id for serve sessions; grep-visible in sessions.db/logs. Coupled to
# the desktop's session filters — rename in both places.
_PLATFORM = "serve"
_DEFAULT_USER = "desktop"

# Cap on a tool_result payload sent over the WS. Aligned with the
# tool_result_storage spill threshold: at or below it the full content IS in
# sessions.db, so nothing is lost by sending it whole; above it the history
# only ever held the preview + side-store path, which /toolresult serves.
_WS_RESULT_LIMIT = 15_000

# Cap on a persisted-reasoning payload sent over the WS (historical 思考 card).
# Reasoning can run long; the desktop collapses it past a preview, and the full
# text stays in sessions.db. Generous so normal thinking survives intact.
_WS_REASONING_LIMIT = 8000

# Cap on tool-call args carried by the live WS event. The agent passes FULL
# args; the emitter slices here and rings the full copy for GET /toolargs
# (live on-demand fetch — history turns get full args via the trace endpoint).
_WS_ARGS_LIMIT = 500


def err(status: int, msg: str) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def run(work):
    return asyncio.get_running_loop().run_in_executor(None, work)


class HttpError(Exception):
    """Raised by executor work; the handler maps it to its status/message."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg
