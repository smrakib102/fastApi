"""Output defence utilities."""

from __future__ import annotations

import re
from typing import Any


_SECRET_KEYS = ("token", "secret", "password", "key")
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
]


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def defend_output(payload: Any) -> Any:
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            key_str = str(key).lower()
            if any(token in key_str for token in _SECRET_KEYS):
                out[key] = "[redacted]"
            else:
                out[key] = defend_output(value)
        return out
    if isinstance(payload, list):
        return [defend_output(item) for item in payload]
    if isinstance(payload, str):
        return _redact_string(payload)
    return payload
