"""Auto-generate short session titles from the user's first message.

Generated *before* the model replies so a title lands on the session even if
that first turn is interrupted, errors out, or hits the iteration cap. Runs on
a daemon thread so it never adds latency to the user-facing reply.
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Upfront path: only the user's opening message is available yet.
_TITLE_PROMPT_UPFRONT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that "
    "starts with the following user message. The title should capture the main "
    "topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the "
    "end, no prefixes."
)

# Exchange path (kept for callers that also have the assistant reply): a
# slightly better title is possible once the first response is in.
_TITLE_PROMPT_EXCHANGE = (
    "Generate a short, descriptive title (3-7 words) for a conversation that "
    "starts with the following exchange. The title should capture the main "
    "topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the "
    "end, no prefixes."
)


def generate_title(user_message: str, assistant_response: str = "",
                   aux=None, timeout: float = None) -> Optional[str]:
    """Generate a session title.

    With only ``user_message`` (the default — assistant_response empty) the
    title is derived from the opening message alone (upfront path). With both,
    it is derived from the first exchange (exchange path). Returns the title
    string or None on failure.
    """
    user_snippet = user_message[:500] if user_message else ""
    assistant_snippet = assistant_response[:500] if assistant_response else ""
    if assistant_snippet:
        prompt = _TITLE_PROMPT_EXCHANGE
        body = f"User: {user_snippet}\n\nAssistant: {assistant_snippet}"
    else:
        prompt = _TITLE_PROMPT_UPFRONT
        body = user_snippet

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": body},
    ]

    try:
        # Reasoning models (title falls back to the main model when no
        # auxiliary.title.model is configured) spend most of the budget on CoT
        # before the short title lands in content. 48 left content empty → raw-msg
        # fallback; needs headroom + a longer timeout than the 10s default.
        response = aux.call_llm(
            task="title",
            messages=messages,
            max_tokens=512,
            temperature=0.3,
            timeout=timeout or 30,
        )
        if not response:
            return None
        # Content only, never reasoning_content — a reasoning model can leave
        # content empty and put its "title" in the reasoning scratchpad, which
        # would pollute the title with "1. **分析请求：** ..." draft text.
        msg = response.choices[0].message
        title = (getattr(msg, "content", None) or "").strip()
        if not title:
            return None
        title = title.strip('"\'')
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None
    except Exception as e:
        logger.debug("Title generation failed: %s", e)
        return None


def maybe_auto_title(session_db, session_id, user_message, aux,
                     conversation_history: list = None) -> None:
    """Fire-and-forget title generation on the session's first user message.

    Gate: this is the session's first user message (the loaded history contains
    no user turn yet) and no title is set yet. Generates from the user message
    alone, *upfront* — so an interrupted/errored first turn still gets a title
    instead of lingering as "新对话".
    """
    if not session_db or not session_id or not user_message:
        return
    # First user message ⇔ history (before this turn) has no user turn yet.
    prior_user_count = sum(
        1 for m in (conversation_history or []) if m.get("role") == "user"
    )
    if prior_user_count > 0:
        return

    thread = threading.Thread(
        target=_auto_title_thread,
        args=(session_db, session_id, user_message, aux),
        daemon=True,
        name="auto-title",
    )
    thread.start()


def _auto_title_thread(session_db, session_id, user_message, aux):
    """Background thread: generate a title from the opening message + save it."""
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:
        return

    title = generate_title(user_message, "", aux)
    if not title:
        # No usable content (reasoning model spent the budget on reasoning) —
        # fall back to the opening line so the session isn't stuck as "新对话".
        title = (user_message or "").strip().split("\n", 1)[0][:30]
    if not title:
        return

    try:
        session_db.set_session_title(session_id, title)
        logger.debug("Auto-generated session title: %s", title)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)
