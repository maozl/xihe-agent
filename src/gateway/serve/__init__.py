"""xihe serve — run xihe as an HTTP + WebSocket service.

Third run mode alongside ``xihe chat`` (interactive CLI) and ``xihe gateway``
(messaging platforms). ``serve`` exposes the *same* agent core over a local
network API so other frontends — the desktop app, a script, a web UI — can drive
xihe without embedding an engine. The desktop connects to ``/stream`` over
WebSocket the way any client connects to a backend.

Architecture mirrors the gateway: one ``SharedContext`` (SQLite / auxiliary LLM
/ compressor, wired once) + a fresh ``XiheAgent`` per turn. Deltas are bridged
from the agent's worker thread to the WebSocket via a stdlib ``queue.Queue`` —
the same pattern as ``gateway.stream_consumer.StreamConsumer``.

Layout: one file per business module, each owning its routes. ``system.py``
(system probes), ``chat.py`` (the /stream turn engine + sessions/history),
``admin.py`` (management panel + specialists + store), ``browser.py``
(browser panel), ``terminal.py`` (ssh terminal panel), ``knowledge.py``
(memory + KBS) — the last two stateless, the rest mixins on ServeApp.
``_common.py`` (identity/wire limits/plain helpers) and ``emitter.py``
(worker→WS bridge) are shared plumbing; ``server.py`` hosts the ServeApp
composition shell AND assembles
and runs the aiohttp app.

REST (stateless):
    GET  /health                          liveness + capability descriptor
    GET  /readiness                       structured what's-missing report (onboarding UIs)
    POST /test-connection                 server-side model-connection probe (key stays server-side)
    GET  /agents                          the xihe "self" agent (P0; personas later)
    GET  /sessions                        serve-platform sessions (most-recent first)
    GET  /convs/{conv_id}/messages        transcript for one conversation
    POST /convs/{conv_id}/reset           start a fresh session round for a conversation
    POST /convs/{conv_id}/truncate        roll back: drop a user row + everything after (resend)
    POST /convs/{conv_id}/title           rename a conversation
    DELETE /convs/{conv_id}               delete a conversation + its transcript
    GET  /specialists                     specialist-agent files (editor view)
    PUT  /specialists/{slug}              write one specialist file
    DELETE /specialists/{slug}            delete one specialist file
    GET  /memory                          long-term memory entries (desktop browser)
    PUT  /memory                          upsert/rename one entry (previous_key)
    DELETE /memory?key=                   delete one entry (?force=1 on a corrupt store)
    GET  /kbs                             KBS digest + page/candidate inventory (read-only)
    GET  /kbs/page?path=                  one KBS markdown page
    GET  /store                           capability-store catalog + install state
    POST /store/install                   install a skill/mcp catalog item
    POST /store/uninstall                 remove a store-installed item
    POST /store/mount                     set which agents an item is mounted to
    POST /store/refresh                   force re-fetch of catalog sources
    GET  /browser/status                  agent CDP Chrome state (panel poll)
    POST /browser/launch                  cold-start the CDP Chrome
    POST /browser/snap                    move Chrome over the desktop panel region
    POST /browser/hide                    hide Chrome while the desktop is unfocused
    POST /browser/show                    re-show + re-place the snapped Chrome
    POST /browser/release                 un-snap: float Chrome beside the desktop
    POST /browser/appearance              desktop pushes light/dark (persists, applies at next launch)
    POST /browser/restart                 kill + relaunch the CDP Chrome (re-apply appearance)

WebSocket  /stream  (streaming turn):
    client → server   {"type":"send","conv_id":...,"text":...}
                      {"type":"attach","conv_id":...}
                      {"type":"interrupt","conv_id":...}
                      {"type":"steer","conv_id":...,"text":...}
                      {"type":"approve","conv_id":...,"id":...,"approved":bool,"always":bool}
    server → client   hello · attached · turn_start · text_delta ·
                      thought_delta · tool_call · tool_result ·
                      approval_request · cron_result · complete · error
                      (cron_result is a PUSH, broadcast to every connected
                      client by DesktopChannel — not part of a turn)

Session mapping: a desktop conversation id (``conv_id``) maps to
``SessionSource(platform="serve", chat_id=conv_id, user_id="desktop",
chat_type="dm")`` → deterministic key ``agent:main:serve:dm:{conv_id}`` → a
persistent xihe session (history lives in ``sessions.db``; survives serve
restarts and shares nothing with CLI/gateway sessions unless you reuse a key).
"""

from gateway.serve.emitter import Emitter
from gateway.serve.server import ServeApp, run_serve

__all__ = ["Emitter", "ServeApp", "run_serve"]
