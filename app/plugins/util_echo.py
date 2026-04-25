"""Reference plugin: a trivial echo tool.

Doubles as a smoke test for the plugin loader. Safe to ship — it has no
side effects and only fires when ``PLUGIN_LOADER_ENABLED=true``.
"""

from __future__ import annotations

from app.plugins.base import Plugin, ToolContext


def _run(args: dict, ctx: ToolContext) -> dict:
    text = args.get("text", "")
    return {
        "echo": str(text),
        "user_id": ctx.user_id,
        "agent_id": ctx.agent_id,
    }


PLUGIN = Plugin(
    name="util.echo",
    category="utility",
    description="Returns the input text. Useful for verifying the plugin loader.",
    handler=_run,
    args_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


def register(registry) -> None:
    registry.add(PLUGIN)
