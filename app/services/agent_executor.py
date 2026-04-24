import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import tools as tool_routes
from app.core.config import settings
from app.worker.celery_app import celery_app
from app.models.approval import Approval

logger = logging.getLogger(__name__)

_tool_circuit: dict[str, dict[str, float]] = {}


class ToolExecutionError(RuntimeError):
    pass


def execute_tool(
    db: Session,
    name: str,
    args: dict,
    internal_user_id: int,
    internal_agent_id: int | None,
    retries: int = 1,
) -> dict:
    _ensure_circuit_open(name)
    _ensure_approval(db, name, args, internal_user_id)
    _validate_tool_schema(db, name, args)

    for attempt in range(retries + 1):
        try:
            result = _execute_via_worker(name, args, internal_user_id, internal_agent_id)
            _record_tool_success(name)
            return result
        except HTTPException as exc:
            logger.warning("tool_execute_http_error", extra={"tool": name, "status": exc.status_code})
            if attempt >= retries:
                _record_tool_failure(name)
                raise ToolExecutionError(str(exc.detail)) from exc
        except Exception as exc:
            logger.warning("tool_execute_error", extra={"tool": name, "error": str(exc)})
            if attempt >= retries:
                _record_tool_failure(name)
                raise ToolExecutionError(str(exc)) from exc

    raise ToolExecutionError("Tool execution failed")


def execute_tool_local(
    db: Session,
    name: str,
    args: dict,
    internal_user_id: int,
    internal_agent_id: int | None,
) -> dict:
    _ensure_approval(db, name, args, internal_user_id)
    _validate_tool_schema(db, name, args)
    payload = tool_routes.ToolExecuteRequest(name=name, arguments=args)
    return _execute_with_timeout(payload, db, name, internal_user_id, internal_agent_id)


def _execute_with_timeout(
    payload: tool_routes.ToolExecuteRequest,
    db: Session,
    tool_name: str,
    internal_user_id: int,
    internal_agent_id: int | None,
) -> dict:
    timeout_seconds = settings.agent_tool_timeout_seconds
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            tool_routes._execute_tool_internal,
            payload,
            db,
            internal_user_id,
            internal_agent_id,
        )
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError as exc:
            logger.warning("tool_execute_timeout", extra={"tool": tool_name, "timeout": timeout_seconds})
            raise ToolExecutionError("Tool execution timed out") from exc


def _execute_via_worker(
    name: str,
    args: dict,
    internal_user_id: int,
    internal_agent_id: int | None,
) -> dict:
    result = celery_app.send_task(
        "app.worker.tasks.execute_tool_task",
        args=[name, args, internal_user_id, internal_agent_id],
        queue="tool_calls",
    )
    try:
        return result.get(timeout=settings.agent_tool_kill_switch_seconds)
    except Exception as exc:
        try:
            result.revoke(terminate=True)
        except Exception:
            pass
        raise ToolExecutionError("Tool worker execution failed") from exc


def _ensure_circuit_open(tool_name: str) -> None:
    state = _tool_circuit.get(tool_name)
    if not state:
        return
    open_until = state.get("open_until", 0)
    if time.monotonic() < open_until:
        raise ToolExecutionError("Tool circuit breaker open")


def _record_tool_failure(tool_name: str) -> None:
    failures = _tool_circuit.get(tool_name, {}).get("failures", 0) + 1
    state = _tool_circuit.setdefault(tool_name, {})
    state["failures"] = failures
    if failures >= settings.agent_tool_circuit_breaker_failures:
        state["open_until"] = time.monotonic() + settings.agent_tool_circuit_cooldown_seconds


def _record_tool_success(tool_name: str) -> None:
    if tool_name in _tool_circuit:
        _tool_circuit.pop(tool_name, None)


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


def _ensure_approval(db: Session, name: str, args: dict, internal_user_id: int) -> None:
    if name != "gmail.send":
        return

    draft_id = args.get("draft_id")
    if not draft_id:
        raise ToolExecutionError("Missing draft_id for approval")

    approvals = db.execute(
        select(Approval)
        .where(
            Approval.user_id == int(internal_user_id),
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
