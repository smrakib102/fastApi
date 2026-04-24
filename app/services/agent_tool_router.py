from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.agent_run_step import AgentRunStep
from app.services.agent_analytics import get_tool_performance


@dataclass
class ToolScore:
    tool: str
    score: float
    breakdown: dict[str, float]
    reasons: list[str]


def rank_tools(
    db: Session,
    user_id: int,
    agent_id: int,
    tools: list[str],
    context_text: str,
    recent_steps: list[AgentRunStep],
    candidates: list[str] | None = None,
) -> list[ToolScore]:
    if candidates:
        tools = [tool for tool in tools if tool in candidates]

    success_tools = {step.tool_name for step in recent_steps if step.status == "success" and step.tool_name}
    failed_tools = {step.tool_name for step in recent_steps if step.status == "failed" and step.tool_name}

    context_tokens = _tokenize(context_text)
    scored: list[ToolScore] = []
    performance_map = get_tool_performance(db, user_id, agent_id, tools)

    for tool in tools:
        breakdown: dict[str, float] = {}
        reasons: list[str] = []
        score = 0.0
        tool_tokens = _tokenize(tool)
        context_match = 0.4 * len(tool_tokens.intersection(context_tokens))
        breakdown["context_match"] = context_match
        score += context_match

        if tool in success_tools:
            breakdown["recent_success"] = 0.6
            score += 0.6
            reasons.append("Recent step success")
        if tool in failed_tools:
            breakdown["recent_failure"] = -0.4
            score -= 0.4
            reasons.append("Recent step failure")

        overlap = 0.1 * _token_overlap(tool, context_text)
        breakdown["name_overlap"] = overlap
        score += overlap

        perf = performance_map.get(tool)
        if perf:
            success_rate = float(perf.success_rate or 0.0)
            breakdown["historical_success"] = 0.7 * success_rate
            breakdown["historical_score"] = 0.4 * (float(perf.score or 0.5) - 0.5)
            score += breakdown["historical_success"]
            score += breakdown["historical_score"]
            reasons.append("Historical success rate")
            if (perf.failure_count or 0) >= 3 and success_rate < 0.4:
                breakdown["failure_penalty"] = -0.5
                score -= 0.5
                reasons.append("Repeated failures")
        scored.append(ToolScore(tool=tool, score=score, breakdown=breakdown, reasons=reasons))

    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {token for token in re.split(r"[^a-zA-Z0-9]+", text.lower()) if token}


def _token_overlap(tool: str, context_text: str) -> int:
    tool_parts = _tokenize(tool)
    context_parts = _tokenize(context_text)
    return len(tool_parts.intersection(context_parts))
