"""System plugins (Phase 5b).

Read-only introspection. Anything that could leak credentials is filtered
out. Designed for diagnostics, not for production workloads.
"""

from __future__ import annotations

import os
import platform
import sys

from app.plugins.base import Plugin, ToolContext


# Environment variables whose names match any of these substrings are
# redacted entirely, regardless of value.
_REDACT_SUBSTRINGS = (
    "key",
    "secret",
    "token",
    "password",
    "passwd",
    "auth",
    "cookie",
    "credential",
    "private",
)


def _is_sensitive(name: str) -> bool:
    lname = name.lower()
    return any(sub in lname for sub in _REDACT_SUBSTRINGS)


def _system_info(args: dict, ctx: ToolContext) -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
    }


PLUGIN_SYSTEM_INFO = Plugin(
    name="system.info",
    category="system",
    description="Return non-sensitive interpreter/platform info.",
    handler=_system_info,
)


def _env_keys(args: dict, ctx: ToolContext) -> dict:
    """List environment variable NAMES only (no values), with sensitive
    entries flagged. Never returns secret values, even if asked.
    """
    items = []
    for name in sorted(os.environ.keys()):
        items.append({"name": name, "sensitive": _is_sensitive(name)})
    return {"count": len(items), "items": items}


PLUGIN_ENV_KEYS = Plugin(
    name="system.env_keys",
    category="system",
    description="List environment variable names (values redacted; sensitive flag included).",
    handler=_env_keys,
)


def register(registry) -> None:
    registry.add(PLUGIN_SYSTEM_INFO)
    registry.add(PLUGIN_ENV_KEYS)
