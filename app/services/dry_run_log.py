"""Dry-run log writer (shadow).

Records simulated dry-run entries without blocking tool execution.
"""

from __future__ import annotations

import json
import logging
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


def record_shadow_dry_run(
    db: Session,
    *,
    user_id: int | None,
    agent_id: int | None,
    run_id: int | None,
    step_index: int | None,
    tool_name: str,
    args: dict,
    reason: str | None = None,
) -> None:
    try:
        args_payload = json.dumps(_redact_args(args), ensure_ascii=True)
        simulated = {
            "status": "shadow",
            "note": "Dry-run shadow log only; tool executed normally.",
        }
        db.execute(
            text(
                """
                INSERT INTO tool_dry_run_log (
                    user_id, agent_id, run_id, step_index,
                    tool_name, args_redacted, simulated_result,
                    status, reason
                ) VALUES (
                    :user_id, :agent_id, :run_id, :step_index,
                    :tool_name, :args_redacted, :simulated_result,
                    :status, :reason
                )
                """
            ),
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "step_index": step_index,
                "tool_name": tool_name,
                "args_redacted": args_payload,
                "simulated_result": json.dumps(simulated, ensure_ascii=True),
                "status": "shadow",
                "reason": reason,
            },
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dry_run_shadow_record_failed",
            extra={"tool": tool_name, "error": str(exc)[:200]},
        )
