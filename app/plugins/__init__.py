"""Plugin loader (Phase 5).

Lightweight, additive plugin system for tool implementations.

Goals:
- Drop a Python file into ``app/plugins/`` (or a subpackage) that exposes a
  module-level ``register(registry)`` callable, and the tools it adds become
  available to ``_execute_tool_internal`` automatically.
- Existing hard-coded tools in ``app/api/routes/tools.py`` keep working
  unchanged. The registry is consulted FIRST; if no plugin claims the
  tool name, the legacy if/elif chain runs.
- Behind a feature flag (``PLUGIN_LOADER_ENABLED``) so a deployment can
  opt-in. Default is off → identical behavior to pre-Phase-5.

Plugin contract (see ``app/plugins/base.py``):

    from app.plugins.base import Plugin, ToolContext

    def _run(args: dict, ctx: ToolContext) -> dict:
        ...

    PLUGIN = Plugin(
        name="my.tool",
        category="custom",
        description="What it does",
        handler=_run,
        required_scopes=[],   # optional
    )

    def register(registry):
        registry.add(PLUGIN)
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Iterable, Optional

from app.plugins.base import Plugin, PluginExecutionError, ToolContext


logger = logging.getLogger(__name__)


class PluginRegistry:
    """In-process registry. Stateless beyond its dict; safe to share."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._discovered = False

    # ---- registration --------------------------------------------------
    def add(self, plugin: Plugin) -> None:
        if not plugin.name or not callable(plugin.handler):
            raise ValueError("Plugin requires a name and a callable handler")
        existing = self._plugins.get(plugin.name)
        if existing is not None and existing is not plugin:
            logger.warning(
                "plugin_overwrite",
                extra={"name": plugin.name, "previous": str(existing)},
            )
        self._plugins[plugin.name] = plugin

    def remove(self, name: str) -> None:
        self._plugins.pop(name, None)

    # ---- lookup --------------------------------------------------------
    def get(self, name: str) -> Optional[Plugin]:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return sorted(self._plugins.keys())

    def all(self) -> Iterable[Plugin]:
        return list(self._plugins.values())

    # ---- discovery -----------------------------------------------------
    def discover(self, *, force: bool = False) -> int:
        """Walk ``app.plugins`` and import every submodule that exposes
        ``register(registry)``. Returns the count of plugins added.

        Idempotent: a second call without ``force=True`` is a no-op.
        Safe in production: discovery failures are logged, not raised.
        """
        if self._discovered and not force:
            return 0

        before = len(self._plugins)
        package_name = __name__  # "app.plugins"
        package = importlib.import_module(package_name)
        package_path = list(getattr(package, "__path__", []))

        for module_info in pkgutil.walk_packages(package_path, prefix=package_name + "."):
            mod_name = module_info.name
            short = mod_name.rsplit(".", 1)[-1]
            # Skip private modules and the base/registry itself.
            if short.startswith("_") or short in {"base"}:
                continue
            try:
                module = importlib.import_module(mod_name)
            except Exception:  # noqa: BLE001 — bad plugin must not break boot
                logger.exception("plugin_import_failed", extra={"module": mod_name})
                continue
            register = getattr(module, "register", None)
            if not callable(register):
                continue
            try:
                register(self)
            except Exception:  # noqa: BLE001
                logger.exception("plugin_register_failed", extra={"module": mod_name})

        self._discovered = True
        added = len(self._plugins) - before
        logger.info(
            "plugin_discover_complete",
            extra={"added": added, "total": len(self._plugins)},
        )
        return added


# Module-level singleton.
plugin_registry = PluginRegistry()


__all__ = [
    "Plugin",
    "PluginExecutionError",
    "PluginRegistry",
    "ToolContext",
    "plugin_registry",
]
