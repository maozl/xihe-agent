"""Web tools — web_search, web_extract, web_crawl.

Supports multiple providers:
  - Firecrawl (FIRECRAWL_API_KEY)
  - Tavily (TAVILY_API_KEY)
  - SerpAPI (SERPAPI_KEY)
  - Bing (BING_SEARCH_API_KEY)
  - Fallback: direct HTTP scraping
"""

import logging
import re
import socket
import ipaddress
from urllib.parse import urlparse
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

try:
    import httpx
    HTTPX_OK = True
except ImportError:
    HTTPX_OK = False


def _check_web() -> bool:
    return HTTPX_OK


def _web_keys(config: dict | None = None) -> dict:
    """Return {provider: api_key} from config.yaml's ``web`` section (empties
    dropped). Providers: tavily, serpapi, bing, firecrawl. Handlers pass their
    parent_agent's config (cheap); check_fns / helpers with no agent pass None
    to load_config() once. Single config source — no env fallback."""
    if config is None:
        try:
            from core.config import load_config
            config = load_config()
        except Exception:
            return {}
    web = config.get("web") or {}
    out: dict[str, str] = {}
    for prov in ("tavily", "serpapi", "bing", "firecrawl"):
        k = (web.get(prov) or {}).get("api_key")
        if k:
            out[prov] = str(k)
    return out


def _has_search_key() -> bool:
    """web_search needs an external search API key — without one it always
    errors. Don't expose it (and pay its system-prompt tax) if unconfigured."""
    return HTTPX_OK and bool(_web_keys())


def _has_firecrawl() -> bool:
    """web_extract/web_crawl's only working path is Firecrawl (their local
    fallback is SSRF-blocked for internal IPs and unreachable for external on
    an airgapped network). Plus URL fetching is now covered by the `http` tool.
    Hide them unless Firecrawl is configured."""
    return HTTPX_OK and bool(_web_keys().get("firecrawl"))


# Hostnames that should always be blocked regardless of IP resolution
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# CGNAT range (100.64.0.0/10) not covered by ipaddress.is_private
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP should be blocked for SSRF protection."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def _is_private_url(url: str) -> bool:
    """Block URLs pointing to private/internal networks (SSRF protection).

    Resolves the hostname to an IP via DNS and checks against private ranges.
    Fails closed: DNS errors and unexpected exceptions block the request.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return True

        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked request to internal hostname: %s", hostname)
            return True

        # Quick string-based checks (no DNS needed)
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return True
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return True

        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            # DNS resolution failed — fail closed
            logger.warning("Blocked request — DNS resolution failed for: %s", hostname)
            return True

        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                logger.warning(
                    "Blocked request to private/internal address: %s -> %s",
                    hostname, ip_str,
                )
                return True

        return False

    except Exception as exc:
        # Fail closed on unexpected errors
        logger.warning("Blocked request — URL safety check error for %s: %s", url, exc)
        return True


def _strip_secrets_from_url(url: str) -> str:
    """Remove API keys/tokens embedded in URLs."""
    return re.sub(r'([?&])(?:key|token|secret|api_key|apikey|password|pwd)=([^&]+)', r'\1\2=***REDACTED***', url, flags=re.IGNORECASE)


def _web_search(args: dict, **kw) -> str:
    query = args.get("query", "")
    if not query:
        return tool_error("query is required")
    max_results = int(args.get("max_results", 5))

    keys = _web_keys(getattr(kw.get("parent_agent"), "config", None))
    if keys.get("tavily"):
        return _tavily_search(query, keys["tavily"], max_results)
    if keys.get("serpapi"):
        return _serpapi_search(query, keys["serpapi"], max_results)
    if keys.get("bing"):
        return _bing_search(query, keys["bing"], max_results)
    if keys.get("firecrawl"):
        return _firecrawl_search(query, keys["firecrawl"], max_results)

    return tool_error("No search API key configured. Set web.tavily / web.serpapi / web.bing / web.firecrawl .api_key in config.yaml.")


def _tavily_search(query: str, api_key: str, max_results: int) -> str:
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:500],
            })
        return tool_result(results=results)
    except Exception as e:
        return tool_error(f"Tavily search failed: {e}")


def _serpapi_search(query: str, api_key: str, max_results: int) -> str:
    try:
        resp = httpx.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": api_key, "engine": "google", "num": max_results},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("organic_results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "content": r.get("snippet", "")[:500],
            })
        return tool_result(results=results)
    except Exception as e:
        return tool_error(f"SerpAPI search failed: {e}")


def _bing_search(query: str, api_key: str, max_results: int) -> str:
    try:
        resp = httpx.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("webPages", {}).get("value", []):
            results.append({
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "content": r.get("snippet", "")[:500],
            })
        return tool_result(results=results)
    except Exception as e:
        return tool_error(f"Bing search failed: {e}")


def _firecrawl_search(query: str, api_key: str, max_results: int) -> str:
    try:
        resp = httpx.post(
            "https://api.firecrawl.dev/v1/search",
            json={"query": query, "limit": max_results},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("data", data.get("results", [])):
            results.append({
                "title": r.get("metadata", {}).get("title", r.get("title", "")),
                "url": r.get("url", r.get("metadata", {}).get("sourceURL", "")),
                "content": (r.get("markdown", "") or r.get("content", ""))[:500],
            })
        return tool_result(results=results)
    except Exception as e:
        return tool_error(f"Firecrawl search failed: {e}")


def _web_extract(args: dict, **kw) -> str:
    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        url = args.get("url", "")
        if url:
            urls = [url]
        else:
            return tool_error("urls is required")

    if len(urls) > 5:
        return tool_error("Maximum 5 URLs per call")

    firecrawl_key = _web_keys(getattr(kw.get("parent_agent"), "config", None)).get("firecrawl")
    results = []
    for url in urls:
        if _is_private_url(url):
            results.append({"url": url, "content": "", "error": "Blocked: URL targets a private or internal network address"})
            continue
        if re.search(r'(?:key|token|secret|api_key|apikey|password)=', url, re.IGNORECASE):
            results.append({"url": _strip_secrets_from_url(url), "content": "", "error": "Blocked: URL contains what appears to be an API key or token"})
            continue
        results.append(_extract_single_url(url, firecrawl_key))

    return tool_result(results=results)


def _extract_single_url(url: str, firecrawl_key: str | None = None) -> dict:
    if firecrawl_key:
        try:
            resp = httpx.post(
                "https://api.firecrawl.dev/v1/scrape",
                json={"url": url, "formats": ["markdown"]},
                headers={"Authorization": f"Bearer {firecrawl_key}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("data", {}).get("markdown", "")
            if content:
                return {"url": url, "content": content[:50000], "source": "firecrawl"}
        except Exception as e:
            logger.debug("Firecrawl extract failed: %s", e)

    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; XiheAgent/1.0)"})
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        if "application/json" in content_type:
            text = resp.text[:50000]
        else:
            text = _html_to_text(resp.text)

        if len(text) > 50000:
            text = text[:50000] + "\n...[truncated]..."
        return {"url": url, "content": text, "source": "direct"}
    except Exception as e:
        return {"url": url, "content": "", "error": f"Failed to fetch: {e}"}


def _web_crawl(args: dict, **kw) -> str:
    url = args.get("url", "")
    if not url:
        return tool_error("url is required")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if _is_private_url(url):
        return tool_result(results=[{"url": url, "content": "", "error": "Blocked: URL targets a private or internal network address"}])

    max_pages = min(int(args.get("max_pages", 10)), 20)

    keys = _web_keys(getattr(kw.get("parent_agent"), "config", None))
    firecrawl_key = keys.get("firecrawl")
    if firecrawl_key:
        try:
            resp = httpx.post(
                "https://api.firecrawl.dev/v1/crawl",
                json={"url": url, "limit": max_pages, "scrapeOptions": {"formats": ["markdown"]}},
                headers={"Authorization": f"Bearer {firecrawl_key}"},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("data", []):
                metadata = item.get("metadata", {})
                results.append({
                    "url": metadata.get("sourceURL", url),
                    "title": metadata.get("title", ""),
                    "content": item.get("markdown", "")[:30000],
                })
            return tool_result(results=results, pages_crawled=len(results))
        except Exception as e:
            logger.debug("Firecrawl crawl failed: %s", e)

    tavily_key = keys.get("tavily")
    if tavily_key:
        try:
            resp = httpx.post(
                "https://api.tavily.com/crawl",
                json={"api_key": tavily_key, "url": url, "limit": max_pages, "extract_depth": "basic"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("results", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "content": (item.get("raw_content", "") or item.get("content", ""))[:30000],
                })
            return tool_result(results=results, pages_crawled=len(results))
        except Exception as e:
            logger.debug("Tavily crawl failed: %s", e)

    result = _extract_single_url(url)
    result["_hint"] = "Full site crawling requires Firecrawl or Tavily API key. Only the single page was extracted."
    return tool_result(results=[result])


def _html_to_text(html: str) -> str:
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = html.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    html = re.sub(r'\s+', ' ', html).strip()
    return html


registry.register(
    name="web_search",
    schema={
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Returns top results with titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results to return (default: 5)"},
                },
                "required": ["query"],
            },
        },
    },
    handler=lambda args, **kw: _web_search(args, **kw),
    check_fn=_has_search_key,
    toolset="web",
    read_only=True,
    description_modifier=lambda desc, avail: (
        desc + " Use web_extract to get full page content from search results."
        if "web_extract" in avail else desc
    ),
)

registry.register(
    name="web_extract",
    schema={
        "type": "function",
        "function": {
            "name": "web_extract",
            "description": (
                "Extract content from web page URLs. Supports batch extraction (up to 5 URLs). "
                "Also works with PDF URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to extract (max 5)",
                        "maxItems": 5,
                    },
                    "url": {"type": "string", "description": "Single URL (alternative to urls array)"},
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _web_extract(args, **kw),
    check_fn=_has_firecrawl,
    toolset="web",
    read_only=True,
    description_modifier=lambda desc, avail: (
        desc + " For JavaScript-heavy pages, use the browser tool instead."
        if "browser" in avail else desc
    ),
)

registry.register(
    name="web_crawl",
    schema={
        "type": "function",
        "function": {
            "name": "web_crawl",
            "description": (
                "Crawl a website and extract content from multiple pages. "
                "Requires Firecrawl or Tavily API key for full site crawling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Base URL to crawl"},
                    "max_pages": {"type": "integer", "description": "Max pages to crawl (default: 10, max: 20)"},
                },
                "required": ["url"],
            },
        },
    },
    handler=lambda args, **kw: _web_crawl(args, **kw),
    check_fn=_has_firecrawl,
    toolset="web",
    read_only=True,
)
