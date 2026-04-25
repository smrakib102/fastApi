"""Phase 5/P7: Gmail tool plugins.

Thin wrappers around the existing helpers in ``app.api.routes.tools``.
Keeping the wire-level logic in the routes module for now means we don't
duplicate Gmail HTTP code; once the legacy if/elif chain is deleted the
helpers can be moved into a dedicated service module.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.plugins.base import Plugin, PluginExecutionError, ToolContext


def _wrap(handler):
    """Translate FastAPI HTTPException → PluginExecutionError."""

    def _inner(args: dict, ctx: ToolContext) -> dict:
        try:
            return handler(args, ctx)
        except PluginExecutionError:
            raise
        except HTTPException as exc:
            raise PluginExecutionError(str(exc.detail), status_code=exc.status_code) from exc

    return _inner


def _draft(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_draft

    return _gmail_draft(args, ctx.db, ctx.user_id)


def _send_request(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_send_request

    return _gmail_send_request(args, ctx.db, ctx.user_id, ctx.agent_id)


def _send(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_send

    return _gmail_send(args, ctx.db, ctx.user_id)


def _profile(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_profile

    return _gmail_profile(ctx.db, ctx.user_id)


def _list_messages(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_list_messages

    return _gmail_list_messages(args, ctx.db, ctx.user_id)


def _list_drafts(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _gmail_list_drafts

    return _gmail_list_drafts(args, ctx.db, ctx.user_id)


def register(registry) -> None:
    registry.add(
        Plugin(
            name="gmail.draft",
            handler=_wrap(_draft),
            category="email",
            description="Create a Gmail draft.",
            required_scopes=["gmail.compose"],
        )
    )
    registry.add(
        Plugin(
            name="gmail.send_request",
            handler=_wrap(_send_request),
            category="email",
            description="Create an approval request to send a Gmail draft.",
            required_scopes=["gmail.send"],
        )
    )
    registry.add(
        Plugin(
            name="gmail.send",
            handler=_wrap(_send),
            category="email",
            description="Send an existing Gmail draft.",
            required_scopes=["gmail.send"],
        )
    )
    registry.add(
        Plugin(
            name="gmail.profile",
            handler=_wrap(_profile),
            category="email",
            description="Fetch the authenticated Gmail profile.",
            required_scopes=["gmail.readonly"],
        )
    )
    registry.add(
        Plugin(
            name="gmail.list_messages",
            handler=_wrap(_list_messages),
            category="email",
            description="List Gmail inbox messages with metadata.",
            required_scopes=["gmail.readonly"],
        )
    )
    registry.add(
        Plugin(
            name="gmail.list_drafts",
            handler=_wrap(_list_drafts),
            category="email",
            description="List Gmail drafts.",
            required_scopes=["gmail.readonly"],
        )
    )
