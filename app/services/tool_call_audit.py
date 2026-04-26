"""Tool call audit writer.

Writes to the `tool_call_audit` table introduced in Step 2. This is
best-effort: failures are logged and never interrupt tool execution.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_args(args: dict) -> dict:
    redacted = {}
    for key, value in (args or {}).items():
        key_str = str(key).lower()
        if any(token in key_str for token in ("token", "secret", "password", "key")):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = _redact_value(value)
    return redacted


def record_tool_call(
    db: Session,
    *,
    user_id: int | None,
    agent_id: int | None,
    run_id: int | None,
    step_index: int | None,
    tool_name: str,
    tool_category: str | None,
    source: str,
    mode: str,
    args: dict,
    result: dict | None,
    status: str,
    error_class: str | None,
    error_message: str | None,
    latency_ms: int | None,
    kernel_decisions: dict | None,
    hitl_required: bool = False,
    hitl_resolution: str | None = None,
    token_cost: int | None = None,
    dollar_cost: float | None = None,
    meta_json: dict | None = None,
) -> None:
    try:
        args_payload = json.dumps(_redact_args(args), ensure_ascii=True)
        result_payload = json.dumps(result, ensure_ascii=True) if result is not None else None
        kernel_payload = json.dumps(kernel_decisions or {}, ensure_ascii=True)
        meta_payload = json.dumps(meta_json or {}, ensure_ascii=True)

        db.execute(
            text(
                """
                INSERT INTO tool_call_audit (
                    user_id, agent_id, run_id, step_index,
                    tool_name, tool_category, source, mode,
                    args_redacted, result_redacted,
                    status, error_class, error_message, latency_ms,
                    kernel_decisions, hitl_required, hitl_resolution,
                    token_cost, dollar_cost, meta_json
                ) VALUES (
                    :user_id, :agent_id, :run_id, :step_index,
                    :tool_name, :tool_category, :source, :mode,
                    :args_redacted, :result_redacted,
                    :status, :error_class, :error_message, :latency_ms,
                    :kernel_decisions, :hitl_required, :hitl_resolution,
                    :token_cost, :dollar_cost, :meta_json
                )
                """
            ),
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "step_index": step_index,
                "tool_name": tool_name,
                "tool_category": tool_category,
                "source": source,
                "mode": mode,
                "args_redacted": args_payload,
                "result_redacted": result_payload,
                "status": status,
                "error_class": error_class,
                "error_message": error_message,
                "latency_ms": latency_ms,
                "kernel_decisions": kernel_payload,
                "hitl_required": hitl_required,
                "hitl_resolution": hitl_resolution,
                "token_cost": token_cost,
                "dollar_cost": dollar_cost,
                "meta_json": meta_payload,
            },
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tool_call_audit_failed",
            extra={"tool": tool_name, "error": str(exc)[:200]},
        )


def should_audit(validation_mode: str, intent_mode: str) -> bool:
    return validation_mode != "off" or intent_mode != "off"


def now_ms(start: float | None) -> int | None:
    if start is None:
        return None
    return int((time.monotonic() - start) * 1000)
