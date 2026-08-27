"""Model catalog: known context lengths + live ``/models`` discovery.

The OpenAI-compatible protocol's ``GET {base_url}/models`` returns model IDs
only — the schema (id/object/created/owned_by) carries no context window, and
vendors keep that metadata in console docs or proprietary APIs (Ark's
InnerDescribeModelEndpoints needs separate Volcano-cloud signing; Zhipu has no
public equivalent). Context length is therefore resolved in layers, each
overriding the ones below:

    config ``models:`` entry  >  embedded ``-32k``-style suffix  >
    _TABLE (longest-prefix-first)  >  caller's default (128k)

Numbers deliberately bias low: over-claiming starves the compressor until the
API overflows, under-claiming only compresses a bit early. Vendors ship new
sizes constantly — the durable fix for a wrong entry is a config ``models:``
override, which always wins.
"""

import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

# One prefix table: startswith matching subsumes both exact names ("glm-4.6"
# matches itself and dated variants like "glm-4.6-250123") and family
# fallbacks ("glm-" → any other GLM). Sorted longest-key-first at import so a
# specific entry never gets shadowed by its own family prefix (deepseek-v3.1
# would lose to deepseek-v3 otherwise) — append entries in any order.
_TABLE: list[tuple[str, int]] = [
    # Zhipu BigModel
    ("glm-4-long", 1_000_000),
    ("glm-4.5v", 64_000),
    ("glm-4.6", 200_000),
    ("glm-4.5", 128_000),
    ("glm-", 128_000),
    # Volcano Ark
    ("doubao-seed-1-6", 256_000),
    ("doubao-seed-1.6", 256_000),
    ("doubao-1-5-pro", 256_000),
    ("doubao-1.5-pro", 256_000),
    ("doubao-", 128_000),
    # DeepSeek
    ("deepseek-v3.1", 128_000),
    ("deepseek-v3.2", 128_000),
    ("deepseek-chat", 64_000),
    ("deepseek-reasoner", 64_000),
    ("deepseek-v3", 64_000),
    ("deepseek-", 64_000),
    # Moonshot
    ("kimi-k2-thinking", 256_000),
    ("kimi-k2", 128_000),
    ("kimi-", 128_000),
    ("moonshot-v1", 128_000),
    # OpenAI
    ("gpt-4.1", 1_000_000),
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-5-mini", 400_000),
    ("gpt-5", 400_000),
    ("gpt-3.5-turbo", 16_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("gemini-", 1_000_000),
]
_TABLE.sort(key=lambda e: -len(e[0]))

_SUFFIX_RE = re.compile(r"-(\d+)([km])$", re.IGNORECASE)


def lookup_context_length(model: str, models_cfg: dict | None = None) -> int | None:
    """Resolve a model's context length; None = unknown (caller defaults).

    ``models_cfg`` is the config ``models:`` section — an explicit
    ``context_length`` there always wins over the built-in layers.
    """
    if not model:
        return None
    if models_cfg:
        entry = models_cfg.get(model)
        if isinstance(entry, dict) and entry.get("context_length"):
            return int(entry["context_length"])
    # Suffix before the table: a name that spells out its window
    # (-32k/-256k) states it more precisely than any family prefix
    # (doubao-pro-32k must not resolve to the doubao- default).
    m = _SUFFIX_RE.search(model)
    if m:
        size = int(m.group(1)) * (1000 if m.group(2).lower() == "k" else 1_000_000)
        if 4_000 <= size <= 10_000_000:  # reject absurd parses like -2k/-8000m
            return size
    for prefix, length in _TABLE:
        if model.startswith(prefix):
            return length
    return None


# Live ID discovery. Only user-initiated paths call this (/model, model_info),
# but it is still a network round-trip inside a long-lived gateway process —
# cached per (base_url, api_key) with a TTL, failures cached briefly so an
# endpoint without /models doesn't stall every /model call.
_DISCOVERY_TIMEOUT = 5.0
_DISCOVERY_TTL = 600.0
_FAILURE_TTL = 60.0
_discovery_cache: dict[str, dict] = {}
_discovery_lock = threading.Lock()


def _fetch_models(base_url: str, api_key: str) -> list[str]:
    """Actually hit ``GET {base_url}/models``. Separated for test injection."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url,
                    timeout=_DISCOVERY_TIMEOUT, max_retries=0)
    return sorted({m.id for m in client.models.list() if getattr(m, "id", None)})


def discover_models(base_url: str, api_key: str, *, force: bool = False) -> list[str]:
    """Model IDs from the endpoint's /models, cached; [] on any failure.

    Many OpenAI-compatible gateways (including internal ones) don't implement
    /models — a failure is a normal outcome, not an error to surface.
    """
    if not base_url or not api_key:
        return []
    # Key on a key-prefix, not the full secret, so the cache never becomes a
    # second copy of the credential.
    cache_key = f"{base_url}|{api_key[:12]}"
    now = time.monotonic()
    with _discovery_lock:
        hit = _discovery_cache.get(cache_key)
        if hit and not force and now - hit["at"] < hit["ttl"]:
            return list(hit["ids"])
    ids: list[str] = []
    ttl = _DISCOVERY_TTL
    try:
        ids = _fetch_models(base_url, api_key)
    except Exception as e:
        logger.debug("model discovery failed for %s: %s", base_url, e)
        ttl = _FAILURE_TTL
    with _discovery_lock:
        _discovery_cache[cache_key] = {"at": time.monotonic(), "ids": ids, "ttl": ttl}
    return list(ids)
