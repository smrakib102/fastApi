"""Automation plugins (Phase 5b).

Glue tools that an agent can chain into multi-step workflows. Kept
side-effect-free (the heavy I/O work is handled by core/system plugins
or by existing OAuth tools in app/api/routes/tools.py).
"""

from __future__ import annotations

import json
from typing import Any

from app.plugins.base import Plugin, PluginExecutionError, ToolContext


# ---------------------------------------------------------------------------
# automation.json_path — pull a field out of a JSON document by a dotted path.
# Lightweight alternative to a full JSONPath implementation; supports:
#   "a.b.c"       -> dict lookups
#   "items.0.name" -> list index by integer
# Missing intermediate keys yield {"value": null, "found": false}.
# ---------------------------------------------------------------------------
def _json_path(args: dict, ctx: ToolContext) -> dict:
    payload = args.get("data")
    path = args.get("path")
    if path is None or not isinstance(path, str):
        raise PluginExecutionError("Missing 'path'")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PluginExecutionError(f"Invalid JSON: {exc}") from exc

    cursor: Any = payload
    parts = [p for p in path.split(".") if p]
    for part in parts:
        if isinstance(cursor, dict):
            if part not in cursor:
                return {"found": False, "value": None}
            cursor = cursor[part]
        elif isinstance(cursor, list):
            try:
                idx = int(part)
            except ValueError:
                return {"found": False, "value": None}
            if idx < 0 or idx >= len(cursor):
                return {"found": False, "value": None}
            cursor = cursor[idx]
        else:
            return {"found": False, "value": None}
    return {"found": True, "value": cursor}


PLUGIN_JSON_PATH = Plugin(
    name="automation.json_path",
    category="automation",
    description="Extract a value from a JSON document by a dotted path (e.g. items.0.name).",
    handler=_json_path,
    args_schema={
        "type": "object",
        "properties": {
            "data": {"description": "JSON object/array or a JSON string"},
            "path": {"type": "string"},
        },
        "required": ["data", "path"],
    },
)


# ---------------------------------------------------------------------------
# automation.template — minimal {{var}} string interpolation. Intentionally
# does NOT support arbitrary expressions; safe for chaining tool outputs.
# ---------------------------------------------------------------------------
import re as _re

_TEMPLATE_VAR_RE = _re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*}}")


def _resolve(vars_: dict, dotted: str) -> Any:
    cursor: Any = vars_
    for part in dotted.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return ""
    return cursor


def _template(args: dict, ctx: ToolContext) -> dict:
    template = args.get("template")
    variables = args.get("variables") or {}
    if not isinstance(template, str):
        raise PluginExecutionError("Missing 'template' string")
    if not isinstance(variables, dict):
        raise PluginExecutionError("'variables' must be an object")

    def _sub(match: _re.Match) -> str:
        return str(_resolve(variables, match.group(1)))

    return {"text": _TEMPLATE_VAR_RE.sub(_sub, template)}


PLUGIN_TEMPLATE = Plugin(
    name="automation.template",
    category="automation",
    description="Render a string template with {{var}} placeholders. Safe (no expressions).",
    handler=_template,
    args_schema={
        "type": "object",
        "properties": {
            "template": {"type": "string"},
            "variables": {"type": "object"},
        },
        "required": ["template"],
    },
)


def register(registry) -> None:
    registry.add(PLUGIN_JSON_PATH)
    registry.add(PLUGIN_TEMPLATE)
