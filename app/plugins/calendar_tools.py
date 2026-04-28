"""Phase 5/P7: Google Calendar tool plugins.

Thin wrappers around the existing helpers in ``app.api.routes.tools``.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.plugins.base import Plugin, PluginExecutionError, ToolContext


def _wrap(handler):
    def _inner(args: dict, ctx: ToolContext) -> dict:
        try:
            return handler(args, ctx)
        except PluginExecutionError:
            raise
        except HTTPException as exc:
            raise PluginExecutionError(str(exc.detail), status_code=exc.status_code) from exc

    return _inner


def _list(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _calendar_list

    return _calendar_list(ctx.db, ctx.user_id)


def _create_request(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _calendar_create_request

    return _calendar_create_request(args, ctx.db, ctx.user_id, ctx.agent_id)


def _create(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _calendar_create

    return _calendar_create(args, ctx.db, ctx.user_id)


def _list_events(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _calendar_list_events

    return _calendar_list_events(args, ctx.db, ctx.user_id)


def _update_request(args: dict, ctx: ToolContext) -> dict:
    from app.api.routes.tools import _calendar_update_request

    return _calendar_update_request(args, ctx.db, ctx.user_id, ctx.agent_id)


def register(registry) -> None:
    registry.add(
        Plugin(
            name="calendar.list",
            handler=_wrap(_list),
            category="calendar",
            description="List the user's Google Calendars.",
            required_scopes=["calendar.readonly"],
        )
    )
    registry.add(
        Plugin(
            name="calendar.list_events",
            handler=_wrap(_list_events),
            category="calendar",
            description="List events from a Google calendar.",
            required_scopes=["calendar.readonly"],
        )
    )
    registry.add(
        Plugin(
            name="calendar.create_request",
            handler=_wrap(_create_request),
            category="calendar",
            description="Create an approval request to add a calendar event.",
            required_scopes=["calendar.events"],
        )
    )
    registry.add(
        Plugin(
            name="calendar.create",
            handler=_wrap(_create),
            category="calendar",
            description="Create a calendar event immediately.",
            required_scopes=["calendar.events"],
        )
    )
    registry.add(
        Plugin(
            name="calendar.update_request",
            handler=_wrap(_update_request),
            category="calendar",
            description="Create an approval request to update a calendar event.",
            required_scopes=["calendar.events"],
        )
    )
