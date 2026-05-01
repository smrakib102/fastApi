from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Set


@dataclass(frozen=True)
class ScopeRegistry:
    version: str
    providers: Dict[str, Dict[str, str]]


_SCOPE_REGISTRY = ScopeRegistry(
    version="2026-05-01",
    providers={
        "google": {
            "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
            "gmail.compose": "https://www.googleapis.com/auth/gmail.compose",
            "gmail.send": "https://www.googleapis.com/auth/gmail.send",
            "calendar.readonly": "https://www.googleapis.com/auth/calendar.readonly",
            "calendar.events": "https://www.googleapis.com/auth/calendar.events",
        }
    },
)


def normalize_scope_list(raw: str | None) -> Set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split() if item.strip()}


def map_required_scopes(provider: str, required_scopes: Iterable[str]) -> Set[str]:
    mapping = _SCOPE_REGISTRY.providers.get(provider, {})
    return {mapping.get(scope, scope) for scope in required_scopes if scope}


def get_registry_version() -> str:
    return _SCOPE_REGISTRY.version


__all__ = [
    "ScopeRegistry",
    "get_registry_version",
    "map_required_scopes",
    "normalize_scope_list",
]
