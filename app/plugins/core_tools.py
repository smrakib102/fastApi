"""Core utility plugins (Phase 5b).

Small, safe building blocks that any agent can use without OAuth or
extra credentials. All handlers are synchronous and side-effect-free
unless explicitly stated.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.plugins.base import Plugin, PluginExecutionError, ToolContext
from app.services.feature_flags import get_bool


# ---------------------------------------------------------------------------
# core.time_now
# ---------------------------------------------------------------------------
def _time_now(args: dict, ctx: ToolContext) -> dict:
    tz = (args.get("timezone") or "utc").lower()
    now = datetime.now(timezone.utc)
    if tz != "utc":
        # Defer real tz support; surface the request so callers can adapt.
        return {
            "iso": now.isoformat(),
            "timezone": "utc",
            "note": f"requested timezone '{tz}' not supported; returned UTC",
        }
    return {"iso": now.isoformat(), "timezone": "utc"}


PLUGIN_TIME_NOW = Plugin(
    name="core.time_now",
    category="core",
    description="Return the current time in UTC (ISO-8601).",
    handler=_time_now,
    args_schema={
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
    },
)


# ---------------------------------------------------------------------------
# core.http_fetch  — GET-only, with hard size + scheme limits.
# ---------------------------------------------------------------------------
_FETCH_MAX_BYTES = 256 * 1024  # 256 KiB
_FETCH_TIMEOUT_SECONDS = 10
_FETCH_ALLOWED_SCHEMES = {"http", "https"}
# Block common SSRF targets (loopback, link-local, metadata).
_BLOCKED_HOST_SUBSTRINGS = (
    "localhost",
    "127.",
    "0.0.0.0",
    "169.254.",
    "metadata.google",
    "::1",
)


def _http_fetch(args: dict, ctx: ToolContext) -> dict:
    if not get_bool("allow_http_tools"):
        raise PluginExecutionError(
            '{"status":"blocked","blocked_reason":"policy_disabled"}',
            status_code=403,
        )
    url = args.get("url")
    if not isinstance(url, str) or not url:
        raise PluginExecutionError("Missing 'url'")

    parsed = urlparse(url)
    if parsed.scheme not in _FETCH_ALLOWED_SCHEMES:
        raise PluginExecutionError("Only http/https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise PluginExecutionError("Invalid URL host")
    for needle in _BLOCKED_HOST_SUBSTRINGS:
        if needle in host:
            raise PluginExecutionError("Host blocked for safety reasons")

    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "ai-agent-system/1.0"})
    except httpx.HTTPError as exc:
        raise PluginExecutionError(f"Fetch failed: {exc}") from exc

    body = resp.content[:_FETCH_MAX_BYTES]
    truncated = len(resp.content) > _FETCH_MAX_BYTES
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = ""
    return {
        "status_code": resp.status_code,
        "url": str(resp.url),
        "content_type": resp.headers.get("content-type", ""),
        "bytes": len(body),
        "truncated": truncated,
        "text": text,
    }


PLUGIN_HTTP_FETCH = Plugin(
    name="core.http_fetch",
    category="core",
    description="HTTP GET a URL and return the (truncated) response body.",
    handler=_http_fetch,
    args_schema={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
)


# ---------------------------------------------------------------------------
# core.text_extract — regex match against an input string.
# ---------------------------------------------------------------------------
_REGEX_MAX_INPUT = 200_000
_REGEX_MAX_MATCHES = 200


def _text_extract(args: dict, ctx: ToolContext) -> dict:
    text = args.get("text")
    pattern = args.get("pattern")
    flags_raw = (args.get("flags") or "").lower()

    if not isinstance(text, str) or not isinstance(pattern, str):
        raise PluginExecutionError("Both 'text' and 'pattern' are required strings")
    if len(text) > _REGEX_MAX_INPUT:
        raise PluginExecutionError("Input text too large")

    flag_value = 0
    if "i" in flags_raw:
        flag_value |= re.IGNORECASE
    if "m" in flags_raw:
        flag_value |= re.MULTILINE
    if "s" in flags_raw:
        flag_value |= re.DOTALL

    try:
        regex = re.compile(pattern, flag_value)
    except re.error as exc:
        raise PluginExecutionError(f"Invalid regex: {exc}") from exc

    matches: list[dict] = []
    for m in regex.finditer(text):
        matches.append({"match": m.group(0), "groups": list(m.groups())})
        if len(matches) >= _REGEX_MAX_MATCHES:
            break
    return {"count": len(matches), "matches": matches}


PLUGIN_TEXT_EXTRACT = Plugin(
    name="core.text_extract",
    category="core",
    description="Run a regex against a string and return matches and capture groups.",
    handler=_text_extract,
    args_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "pattern": {"type": "string"},
            "flags": {"type": "string", "description": "Subset of i/m/s"},
        },
        "required": ["text", "pattern"],
    },
)


def register(registry) -> None:
    registry.add(PLUGIN_TIME_NOW)
    registry.add(PLUGIN_HTTP_FETCH)
    registry.add(PLUGIN_TEXT_EXTRACT)
