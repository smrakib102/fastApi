"""Dry-run executor.

Returns simulated results for tools when dry-run enforcement is enabled.
"""

from __future__ import annotations


def execute_dry_run(tool_name: str, args: dict) -> dict:
    if tool_name == "gmail.send":
        return {
            "status": "dry_run",
            "note": "Would send Gmail draft.",
            "draft_id": args.get("draft_id"),
        }
    if tool_name == "calendar.create_request":
        return {
            "status": "dry_run",
            "note": "Would create calendar event request.",
            "calendar_id": args.get("calendar_id"),
            "summary": args.get("summary"),
        }
    if tool_name == "api.request":
        return {
            "status": "dry_run",
            "note": "Would execute external HTTP request.",
            "method": args.get("method"),
            "url": args.get("url"),
        }
    return {
        "status": "dry_run",
        "note": "Dry-run enforced; tool not executed.",
        "tool": tool_name,
    }
