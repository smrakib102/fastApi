import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import tools as tool_routes
from app.core.config import settings
from app.core.redis_client import get_redis
from app.worker.celery_app import celery_app
from app.models.approval import Approval
from app.services.credential_resolver import CredentialContext, CredentialResult, resolve_credential

logger = logging.getLogger(__name__)

# S3: Circuit breaker state moved to Redis so it survives worker restarts
# and is shared across worker pods. Falls back to local dict if Redis is
# unreachable, so the breaker never silently disables itself.
_tool_circuit: dict[str, dict[str, float]] = {}
_CIRCUIT_KEY = "tool:circuit:{name}"


def _circuit_get(tool_name: str) -> dict[str, float]:
    try:
        raw = get_redis().get(_CIRCUIT_KEY.format(name=tool_name))
        if raw:
            return json.loads(raw)
    except Exception:
        logger.debug("circuit_redis_get_failed", extra={"tool": tool_name})
    return _tool_circuit.get(tool_name, {})


def _circuit_set(tool_name: str, state: dict[str, float], ttl: int) -> None:
    _tool_circuit[tool_name] = state
    try:
        get_redis().setex(
            _CIRCUIT_KEY.format(name=tool_name), max(ttl, 1), json.dumps(state)
        )
    except Exception:
        logger.debug("circuit_redis_set_failed", extra={"tool": tool_name})


def _circuit_clear(tool_name: str) -> None:
    _tool_circuit.pop(tool_name, None)
    try:
        get_redis().delete(_CIRCUIT_KEY.format(name=tool_name))
    except Exception:
        logger.debug("circuit_redis_clear_failed", extra={"tool": tool_name})


class ToolExecutionError(RuntimeError):
    pass


# S7: Tool errors that look like an expired/missing OAuth token are turned
# into a graceful "reconnect required" signal. The first run that hits
# this also files a fresh PermissionService.request so the user gets a
# reconnect card next time the chat surface renders.
_OAUTH_FAIL_HINTS = (
    "missing refresh token",
    "no google account",
    "failed to refresh token",
    "invalid_grant",
    "401",
)


def _looks_like_oauth_failure(detail: str) -> bool:
    if not detail:
        return False
    lo = detail.lower()
    return any(hint in lo for hint in _OAUTH_FAIL_HINTS)


def _trigger_reconnect(db: Session, tool_name: str, user_id: int) -> None:
    """Best-effort: ask PermissionService to surface a reconnect card.
    Never raises — OAuth failure handling must not double-fault."""
    try:
        from app.models.user import User
        from app.services.permission_service import permission_service

        user = db.get(User, user_id)
        if not user:
            return
        permission_service.request(
            db,
            user=user,
            tool_name=tool_name,
            reason=f"{tool_name} access expired — please reconnect.",
        )
        db.flush()
    except Exception:
        logger.debug("oauth_reconnect_request_failed", extra={"tool": tool_name})


def _build_auth_context(result: CredentialResult) -> dict:
    return {
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "source": result.source,
        "scopes": sorted(result.scopes),
        "status": result.status,
        "credential_id": result.credential_id,
        "trace": {
            "selection_reason": result.trace.selection_reason,
            "fallback_triggered": result.trace.fallback_triggered,
            "scope_check_result": result.trace.scope_check_result,
        },
    }


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
            credential_result = resolve_credential(
                CredentialContext(
                    user_id=internal_user_id,
                    agent_id=internal_agent_id,
                    tool_name=name,
                    execution_id=str(uuid.uuid4()),
                    retry_count=attempt,
                )
            )
            auth_context = _build_auth_context(credential_result)
            result = _execute_via_worker(
                name,
                args,
                internal_user_id,
                internal_agent_id,
                auth_context,
            )
            _record_tool_success(name)
            return result
        except HTTPException as exc:
            logger.warning("tool_execute_http_error", extra={"tool": name, "status": exc.status_code})
            if attempt >= retries:
                _record_tool_failure(name)
                if _looks_like_oauth_failure(str(exc.detail)):
                    _trigger_reconnect(db, name, internal_user_id)
                    raise ToolExecutionError(
                        f"Reconnect required for {name}: {exc.detail}"
                    ) from exc
                raise ToolExecutionError(str(exc.detail)) from exc
        except Exception as exc:
            logger.warning("tool_execute_error", extra={"tool": name, "error": str(exc)})
            if attempt >= retries:
                _record_tool_failure(name)
                if _looks_like_oauth_failure(str(exc)):
                    _trigger_reconnect(db, name, internal_user_id)
                    raise ToolExecutionError(
                        f"Reconnect required for {name}: {exc}"
                    ) from exc
                raise ToolExecutionError(str(exc)) from exc

    raise ToolExecutionError("Tool execution failed")


def execute_tool_local(
    db: Session,
    name: str,
    args: dict,
    internal_user_id: int,
    internal_agent_id: int | None,
    auth_context: dict | None = None,
) -> dict:
    _ensure_approval(db, name, args, internal_user_id)
    _validate_tool_schema(db, name, args)
    if auth_context is None:
        credential_result = resolve_credential(
            CredentialContext(
                user_id=internal_user_id,
                agent_id=internal_agent_id,
                tool_name=name,
                execution_id=str(uuid.uuid4()),
                retry_count=0,
            )
        )
        auth_context = _build_auth_context(credential_result)
    payload = tool_routes.ToolExecuteRequest(
        name=name,
        arguments=args,
        auth_context=auth_context,
    )
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
    auth_context: dict,
) -> dict:
    result = celery_app.send_task(
        "app.worker.tasks.execute_tool_task",
        args=[name, args, internal_user_id, internal_agent_id, auth_context],
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
    state = _circuit_get(tool_name)
    if not state:
        return
    open_until = state.get("open_until", 0)
    # Stored as wall-clock epoch seconds (so it's comparable across pods).
    if time.time() < open_until:
        raise ToolExecutionError("Tool circuit breaker open")


def _record_tool_failure(tool_name: str) -> None:
    state = _circuit_get(tool_name) or {}
    failures = int(state.get("failures", 0)) + 1
    state["failures"] = failures
    cooldown = settings.agent_tool_circuit_cooldown_seconds
    ttl = cooldown
    if failures >= settings.agent_tool_circuit_breaker_failures:
        state["open_until"] = time.time() + cooldown
        ttl = cooldown
    else:
        # Keep failure-count window short so transient blips decay.
        ttl = max(cooldown, 60)
    _circuit_set(tool_name, state, ttl)


def _record_tool_success(tool_name: str) -> None:
    _circuit_clear(tool_name)


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
    if settings.plugin_loader_enabled:
        try:
            from app.plugins import plugin_registry

            plugin_registry.discover()
            plugin = plugin_registry.get(name)
            if plugin is not None:
                return plugin.args_schema or {"type": "object", "properties": {}, "required": []}
        except Exception:
            logger.debug("plugin_schema_lookup_failed", extra={"tool": name})

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
