"""HTTP request tool — make REST API calls without shell/urllib boilerplate.

Replaces the recurring pattern of hand-rolling `urllib.request.urlopen(...)` or
`curl` (with their quoting/escaping pain). Returns a structured result
{status, headers, body_text, body_json} so the agent doesn't parse raw output.

Scope: this is for calling APIs (GET/POST/PUT/PATCH/DELETE with headers/json
body), NOT for reading web page content — use web_extract for that.

Security: blocks only cloud metadata endpoints (169.254.169.254,
metadata.google.internal, ECS/Azure IMDS) to prevent prompt-injection-driven
credential theft. Private/internal IPs are ALLOWED (this agent's job is to call
internal services)."""

import json as _json
import logging
import re
import time

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False

_DEFAULT_TIMEOUT = 30.0
_MAX_BODY = 30000          # chars of body_text returned (head+tail truncation)
_MAX_HEADERS = 50

# Only block cloud metadata endpoints (the genuinely dangerous SSRF target).
# Internal/private IPs are intentionally allowed — this agent calls internal APIs.
_METADATA_HOSTS = (
    "169.254.169.254", "metadata.google.internal",
    "169.254.170.2", "169.254.170.3",            # ECS task metadata
    "::ffff:169.254.169.254",                    # IPv4-mapped IPv6 bypass
)
_METADATA_HOST_RE = re.compile(r"(169\.254\.169\.254|metadata\.google\.internal)", re.I)


def _check_http() -> bool:
    return _HTTPX_OK


def _is_metadata(url: str) -> bool:
    return bool(_METADATA_HOST_RE.search(url)) or any(h in url for h in _METADATA_HOSTS)


def _truncate(text: str, limit: int = _MAX_BODY) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    omitted = len(text) - limit
    return head + f"\n\n... [body truncated - {omitted:,} chars omitted] ...\n\n" + tail


def _try_json(text: str):
    """Return parsed JSON if the body looks like JSON, else None."""
    s = text.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return _json.loads(s)
    except Exception:
        return None


def _http(args: dict, **kw) -> str:
    if not _HTTPX_OK:
        return tool_error("httpx not installed. pip install httpx to use the http tool.")

    method = (args.get("method") or "GET").strip().upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return tool_error(f"Unsupported method: {method}")

    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required")
    if not url.lower().startswith(("http://", "https://")):
        return tool_error(f"url must start with http:// or https:// (got: {url})")
    if _is_metadata(url):
        return tool_error(
            "BLOCKED: cloud metadata endpoint (SSRF guard). "
            "Fetching instance metadata is not allowed."
        )

    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return tool_error("headers must be an object")
    params = args.get("params") or None
    if params is not None and not isinstance(params, dict):
        return tool_error("params must be an object")

    json_body = args.get("json")
    data = args.get("data")  # raw text body (alternative to json)
    if json_body is not None and data is not None:
        return tool_error("provide either 'json' or 'data', not both")

    timeout = float(args.get("timeout", _DEFAULT_TIMEOUT))
    timeout = max(1.0, min(timeout, 300.0))
    follow_redirects = args.get("follow_redirects", True)
    verify_ssl = args.get("verify_ssl", True)

    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=bool(follow_redirects),
                          verify=bool(verify_ssl)) as client:
            resp = client.request(
                method, url,
                headers={str(k): str(v) for k, v in headers.items()},
                params=params,
                json=json_body if json_body is not None else None,
                content=data if data is not None else None,
            )
    except httpx.TimeoutException:
        return tool_error(f"{method} {url} timed out after {timeout}s")
    except httpx.ConnectError as e:
        return tool_error(f"Connection failed to {url}: {e}")
    except httpx.HTTPError as e:
        return tool_error(f"{method} {url} failed: {type(e).__name__}: {e}")
    except Exception as e:
        return tool_error(f"{method} {url} failed: {e}")

    elapsed_ms = int((time.monotonic() - start) * 1000)

    try:
        body_text = resp.content.decode("utf-8", errors="replace")
    except Exception:
        body_text = resp.text or ""

    body_json = _try_json(body_text)

    # Headers (cap to avoid huge Set-Cookie spam)
    resp_headers = dict(resp.headers)
    if len(resp_headers) > _MAX_HEADERS:
        resp_headers = dict(list(resp_headers.items())[:_MAX_HEADERS])

    result = {
        "method": method,
        "url": str(resp.url),           # final url (after redirects)
        "status": resp.status_code,
        "reason": resp.reason_phrase,
        "elapsed_ms": elapsed_ms,
        "headers": resp_headers,
        "body_text": _truncate(body_text),
        "body_json": body_json,
        "body_truncated": len(body_text) > _MAX_BODY,
    }
    logger.info("http %s %s -> %d (%dms, %d bytes)",
                method, url, resp.status_code, elapsed_ms, len(body_text))
    return tool_result(**result)


registry.register(
    name="http",
    toolset="http",
    schema={
        "type": "function",
        "function": {
            "name": "http",
            "description": (
                "Make an HTTP/REST API call (GET/POST/PUT/PATCH/DELETE/etc.) and return a "
                "structured result: {status, headers, body_text, body_json}. Use this instead "
                "of curl or hand-rolling urllib — no shell-quoting pain, JSON body/params are "
                "native dicts, JSON responses are auto-parsed. For reading web PAGE content "
                "(articles/docs) use web_extract instead; this is for API calls. Internal IPs "
                "are allowed (this agent calls internal services)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        "description": "HTTP method (default: GET).",
                    },
                    "url": {
                        "type": "string",
                        "description": "Request URL (must start with http:// or https://).",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Request headers (e.g. {\"Authorization\":\"Bearer ...\", \"Content-Type\":\"application/json\"}).",
                    },
                    "params": {
                        "type": "object",
                        "description": "Query string parameters.",
                    },
                    "json": {
                        "description": "JSON body (object or array). Sets Content-Type to application/json. Use this OR 'data'.",
                    },
                    "data": {
                        "type": "string",
                        "description": "Raw request body (text). Use this OR 'json'.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default 30, max 300).",
                    },
                    "follow_redirects": {
                        "type": "boolean",
                        "description": "Follow HTTP redirects (default true).",
                    },
                    "verify_ssl": {
                        "type": "boolean",
                        "description": "Verify TLS certificates (default true). Set false for internal self-signed CAs.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    handler=lambda args, **kw: _http(args, **kw),
    check_fn=_check_http,
    read_only=False,
)
