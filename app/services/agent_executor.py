import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import tools as tool_routes
from app.core.config import settings
from app.models.approval import Approval

logger = logging.getLogger(__name__)


class ToolExecutionError(RuntimeError):
    pass


def execute_tool(
    db: Session,
    name: str,
    args: dict,
    retries: int = 1,
) -> dict:
    _ensure_approval(db, name, args)
    _validate_tool_schema(db, name, args)

    payload = tool_routes.ToolExecuteRequest(name=name, arguments=args)
    for attempt in range(retries + 1):
        try:
            return tool_routes.tool_execute(payload, settings.tool_api_token, db)
        except HTTPException as exc:
            logger.warning("tool_execute_http_error", extra={"tool": name, "status": exc.status_code})
            if attempt >= retries:
                raise ToolExecutionError(str(exc.detail)) from exc
        except Exception as exc:
            logger.warning("tool_execute_error", extra={"tool": name, "error": str(exc)})
            if attempt >= retries:
                raise ToolExecutionError(str(exc)) from exc

    raise ToolExecutionError("Tool execution failed")


def _validate_tool_schema(db: Session, name: str, args: dict) -> None:
    schema = _get_tool_schema(db, name)
    if not schema:
        raise ToolExecutionError(f"Unknown tool schema for {name}")

    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    errors: list[str] = []

    for field in required:
        if field not in args:
            errors.append(f"Missing required field: {field}")

    type_map = {
        "integer": int,
        "string": str,
        "object": dict,
        "array": list,
    }
    for field, meta in properties.items():
        if field not in args:
            continue
        field_type = meta.get("type")
        expected = type_map.get(field_type)
        if expected and not isinstance(args[field], expected):
            errors.append(f"Invalid type for {field}: expected {field_type}")

    if errors:
        raise ToolExecutionError("; ".join(errors))


def _get_tool_schema(db: Session, name: str) -> dict | None:
    manifest = tool_routes.tool_manifest(settings.tool_api_token)
    for tool in manifest.get("tools", []):
        if tool.get("name") == name:
            return tool.get("input_schema") or {}
    return None


def _ensure_approval(db: Session, name: str, args: dict) -> None:
    if name != "gmail.send":
        return

    draft_id = args.get("draft_id")
    user_id = args.get("user_id")
    if not draft_id or not user_id:
        raise ToolExecutionError("Missing draft_id for approval")

    approvals = db.execute(
        select(Approval)
        .where(
            Approval.user_id == int(user_id),
            Approval.type == "gmail.send",
            Approval.status == "approved",
        )
        .order_by(Approval.requested_at.desc())
    ).scalars().all()

    for approval in approvals:
        try:
            payload = json.loads(approval.payload)
        except json.JSONDecodeError:
            continue
        if payload.get("draft_id") == draft_id:
            return

    raise ToolExecutionError("Approval required before sending Gmail draft")
