"""Plugin contract types.

Kept in its own module so plugins can ``from app.plugins.base import ...``
without forcing a registry import (and the side-effects of discovery).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy.orm import Session


class PluginExecutionError(RuntimeError):
    """Raised by a plugin handler to signal a structured failure."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ToolContext:
    """Everything a plugin handler needs at execution time.

    Designed to be channel-agnostic and easy to mock in tests.
    """

    db: Session
    user_id: int
    agent_id: Optional[int] = None
    # Free-form bag for adapters to inject extras (channel, run_id, etc.)
    extras: dict = field(default_factory=dict)


# Plugin handler signature: (args, ctx) -> result_dict.
PluginHandler = Callable[[dict, ToolContext], dict]


@dataclass
class Plugin:
    """Static description + handler for a single tool name.

    ``required_scopes`` is reserved for Phase 5b (it will be cross-checked
    against credentials surfaced via :class:`PermissionService`).
    """

    name: str
    handler: PluginHandler
    category: str = "custom"
    description: str = ""
    required_scopes: list[str] = field(default_factory=list)
    # Optional JSON schema for the args dict; consumed by Phase 6 planner.
    args_schema: Optional[dict] = None

    def __str__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Plugin {self.name} ({self.category})>"


__all__ = [
    "Plugin",
    "PluginHandler",
    "PluginExecutionError",
    "ToolContext",
]
