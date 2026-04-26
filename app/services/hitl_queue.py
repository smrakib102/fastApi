"""HITL shadow queue writer.

Records pending confirmations in shadow mode without blocking execution.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _redact_args(args: dict) -> dict:
    redacted = {}
    for key, value in (args or {}).items():
        key_str = str(key).lower()
        if any(token in key_str for token in ("token", "secret", "password", "key")):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def record_shadow_confirmation(
    db: Session,
    *,
    user_id: int,
    agent_id: int | None,
    run_id: int | None,
    step_index: int | None,
    tool_name: str,
    args: dict,
    reason: str | None,
) -> None:
    try:
        token = secrets.token_hex(16)
        args_payload = json.dumps(_redact_args(args), ensure_ascii=True)
        db.execute(
            text(
                """
                INSERT INTO tool_confirmations (
                    token, user_id, agent_id, run_id, step_index,
                    tool_name, args_redacted, status, reason
                ) VALUES (
                    :token, :user_id, :agent_id, :run_id, :step_index,
                    :tool_name, :args_redacted, :status, :reason
                )
                """
            ),
            {
                "token": token,
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "step_index": step_index,
                "tool_name": tool_name,
                "args_redacted": args_payload,
                "status": "shadow",
                "reason": reason,
            },
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hitl_shadow_record_failed",
            extra={"tool": tool_name, "error": str(exc)[:200]},
        )


def record_pending_confirmation(
    db: Session,
    *,
    user_id: int,
    agent_id: int | None,
    run_id: int | None,
    step_index: int | None,
    tool_name: str,
    args: dict,
    reason: str | None,
) -> str | None:
    try:
        token = secrets.token_hex(16)
        args_payload = json.dumps(_redact_args(args), ensure_ascii=True)
        db.execute(
            text(
                """
                INSERT INTO tool_confirmations (
                    token, user_id, agent_id, run_id, step_index,
                    tool_name, args_redacted, status, reason
                ) VALUES (
                    :token, :user_id, :agent_id, :run_id, :step_index,
                    :tool_name, :args_redacted, :status, :reason
                )
                """
            ),
            {
                "token": token,
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "step_index": step_index,
                "tool_name": tool_name,
                "args_redacted": args_payload,
                "status": "pending",
                "reason": reason,
            },
        )
        db.commit()
        return token
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hitl_pending_record_failed",
            extra={"tool": tool_name, "error": str(exc)[:200]},
        )
        return None
