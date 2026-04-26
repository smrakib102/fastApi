"""Telegram-related built-in tools.

Currently exposes ``telegram.group_summary`` — a thin wrapper around
``app.services.summary_service.generate_summary`` so any agent whose
``config.telegram_chat_id`` is bound can produce an on-demand digest of
the last N hours of group chatter.

The handler tolerates a missing ``chat_id`` argument by falling back to
the calling agent's ``config.telegram_chat_id``. That lets the chat
layer issue a zero-arg direct-tool-call (just the tool name) when the
user types "run agent" — no slot extraction needed.
"""

from __future__ import annotations

import json
import logging

from app.models.agent import Agent
from app.plugins.base import Plugin, PluginExecutionError, ToolContext
from app.services.summary_service import generate_summary

logger = logging.getLogger(__name__)


def _resolve_chat_id(args: dict, ctx: ToolContext) -> str:
    chat_id = (args.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    if ctx.agent_id is None:
        raise PluginExecutionError(
            "chat_id is required (no agent context to fall back to)"
        )
    agent = ctx.db.get(Agent, ctx.agent_id)
    if agent is None:
        raise PluginExecutionError("Agent not found")
    try:
        cfg = json.loads(agent.config) if agent.config else {}
    except Exception:  # noqa: BLE001
        cfg = {}
    bound = (cfg.get("telegram_chat_id") or "").strip() if isinstance(cfg, dict) else ""
    if not bound:
        raise PluginExecutionError(
            "This agent isn't bound to a Telegram group yet. "
            "Add the bot to a group first."
        )
    return bound


def _telegram_group_summary(args: dict, ctx: ToolContext) -> dict:
    chat_id = _resolve_chat_id(args, ctx)
    timezone_name = (args.get("timezone") or "UTC").strip() or "UTC"
    try:
        summary = generate_summary(ctx.db, ctx.user_id, chat_id, timezone_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "telegram_group_summary_failed",
            extra={
                "user_id": ctx.user_id,
                "agent_id": ctx.agent_id,
                "chat_id": chat_id,
            },
        )
        raise PluginExecutionError(f"Could not build summary: {exc}") from exc
    return {"chat_id": chat_id, "summary": summary}


PLUGIN_TG_GROUP_SUMMARY = Plugin(
    name="telegram.group_summary",
    category="telegram",
    description=(
        "Summarise the last 24 hours of messages from the Telegram group "
        "bound to this agent. Pass `chat_id` to override; otherwise it "
        "uses the agent's bound group."
    ),
    handler=_telegram_group_summary,
    args_schema={
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Optional Telegram chat id to summarise.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone, defaults to UTC.",
            },
        },
        "required": [],
    },
)


def register(registry) -> None:
    registry.add(PLUGIN_TG_GROUP_SUMMARY)
