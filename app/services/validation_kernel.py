"""Validation Kernel (shadow-ready).

Currently used in shadow mode to annotate tool calls without blocking.
The logic is intentionally conservative and lightweight. When enforce
mode is enabled later, this module will be extended with stricter checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationDecision:
    status: str  # pass | warn | block
    reasons: list[str]


def evaluate(tool_name: str, args: dict) -> ValidationDecision:
    reasons: list[str] = []

    if not isinstance(args, dict):
        return ValidationDecision(status="block", reasons=["Arguments must be an object"])

    try:
        raw = json.dumps(args, ensure_ascii=True)
    except Exception:
        return ValidationDecision(status="block", reasons=["Arguments must be JSON-serialisable"])

    if len(raw) > 4000:
        reasons.append("Arguments payload is large")

    for key in args.keys():
        if str(key).startswith("__"):
            reasons.append("Arguments contain private-style keys")
            break

    if reasons:
        return ValidationDecision(status="warn", reasons=reasons)

    return ValidationDecision(status="pass", reasons=["Basic validation passed"])
