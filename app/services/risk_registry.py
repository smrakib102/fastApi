"""Risk registry lookup for tool safety classification."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class RiskProfile:
    tool_name: str
    risk_tier: str
    requires_hitl: bool
    requires_dry_run: bool
    description: str | None


_DEFAULT_PROFILE = RiskProfile(
    tool_name="*",
    risk_tier="high",
    requires_hitl=True,
    requires_dry_run=True,
    description="Default high-risk policy for unknown tools",
)


def get_profile(db: Session, tool_name: str) -> RiskProfile:
    try:
        row = db.execute(
            text(
                """
                SELECT tool_name, risk_tier, requires_hitl, requires_dry_run, description
                  FROM tool_risk_profiles
                 WHERE tool_name = :tool
                """
            ),
            {"tool": tool_name},
        ).first()
        if row:
            return RiskProfile(
                tool_name=row[0],
                risk_tier=str(row[1] or "medium"),
                requires_hitl=bool(row[2]),
                requires_dry_run=bool(row[3]),
                description=row[4],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk_registry_read_failed", extra={"tool": tool_name, "error": str(exc)[:200]})

    logger.warning("risk_registry_default_applied", extra={"tool": tool_name})
    return _DEFAULT_PROFILE
