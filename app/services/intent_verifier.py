"""Intent Verifier (shadow-ready).

Determines whether a tool invocation appears aligned with user intent.
In step 4 we run in shadow mode only: record verdicts, do not block.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IntentDecision:
    status: str  # verified | drift | abstain
    reasons: list[str]


def evaluate(
    user_text: str | None,
    tool_name: str,
    *,
    direct_call: bool = False,
    step_thought: str | None = None,
) -> IntentDecision:
    if direct_call:
        return IntentDecision(status="verified", reasons=["Direct tool invocation requested"])

    text = (user_text or "").strip().lower()
    thought = (step_thought or "").strip().lower()

    if not text:
        return IntentDecision(status="abstain", reasons=["No user input to verify against"])

    tool_tokens = [token for token in tool_name.lower().split(".") if token]
    if any(token in text for token in tool_tokens):
        return IntentDecision(status="verified", reasons=["Tool name appears in user input"])

    if thought and any(token in thought for token in tool_tokens):
        return IntentDecision(status="abstain", reasons=["Tool name present in planner rationale only"])

    return IntentDecision(status="abstain", reasons=["No strong intent match"])
