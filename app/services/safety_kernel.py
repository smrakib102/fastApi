"""Safety Kernel (shadow-ready).

Evaluates risk tier and HITL requirements for a tool call. In step 5 we
run in shadow mode only and record decisions without blocking.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.risk_registry import get_profile


@dataclass
class SafetyDecision:
    status: str  # allow | deny
    risk_tier: str
    requires_hitl: bool
    requires_dry_run: bool
    reasons: list[str]


def evaluate(db: Session, tool_name: str) -> SafetyDecision:
    profile = get_profile(db, tool_name)
    reasons: list[str] = []

    if profile.risk_tier == "critical" and profile.requires_hitl and profile.requires_dry_run:
        reasons.append("Critical tool denied by safety policy")
        return SafetyDecision(
            status="deny",
            risk_tier=profile.risk_tier,
            requires_hitl=True,
            requires_dry_run=True,
            reasons=reasons,
        )

    if profile.requires_hitl:
        reasons.append("Tool requires HITL per risk profile")
        return SafetyDecision(
            status="allow",
            risk_tier=profile.risk_tier,
            requires_hitl=True,
            requires_dry_run=profile.requires_dry_run,
            reasons=reasons,
        )

    reasons.append("Risk profile allows execution")
    return SafetyDecision(
        status="allow",
        risk_tier=profile.risk_tier,
        requires_hitl=False,
        requires_dry_run=profile.requires_dry_run,
        reasons=reasons,
    )
