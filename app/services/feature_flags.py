"""Feature flag resolver — v4 governance foundation.

Resolution order (highest authority first):

    1. ``.env`` (Settings) — if explicitly set, wins. This is the emergency
       authority: operators can force a flag via env + restart even if the DB
       has been tampered with or admin UI is unreachable.
    2. Database row in ``feature_flags`` — admin runtime control. Allows
       toggling subsystems without a restart.
    3. Settings default (always OFF / "off" for v4 flags).

For the two emergency switches ``SAFE_MODE_ENABLED`` and
``STRICT_MODE_ENABLED``, the .env value ALWAYS wins over the DB regardless
of whether it is explicitly set, because the safety semantics demand that
operators can force-restart into a known safe state.

Reads are cheap (in-process TTL cache) and gracefully degrade to the
Settings default if the DB is unreachable. Writes go through
``set_db_flag`` and invalidate the cache.

Nothing in this module imports from kernel/tool code, so importing it is
safe at any point in startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# Flags whose .env value ALWAYS wins over the DB. They are the two
# emergency switches; the env representation is the source of truth.
_ENV_AUTHORITATIVE_FLAGS: frozenset[str] = frozenset(
    {
        "safe_mode_enabled",
        "strict_mode_enabled",
    }
)

# Short TTL: low enough for admin toggles to feel responsive, high enough
# to keep DB load trivial under hot tool execution paths.
_CACHE_TTL_SECONDS = 5.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _env_var_name(key: str) -> str:
    return key.upper()


def _env_explicit(key: str) -> bool:
    """True if the operator explicitly set the env var (even to 'false')."""
    return _env_var_name(key) in os.environ


def _settings_value(key: str) -> Any:
    """Read the typed value the Settings class produced for this flag."""
    return getattr(settings, key, None)


def _read_db(key: str) -> Any | None:
    """Look up the flag in the feature_flags table. Returns None on miss
    or any error — callers must always have a usable fallback."""
    try:
        from sqlalchemy import text
        from app.db.session import SessionLocal
    except Exception:  # pragma: no cover — import guard
        return None

    try:
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT value_json FROM feature_flags WHERE key = :k"),
                {"k": key},
            ).first()
    except Exception as exc:
        logger.warning(
            "feature_flags_db_read_failed",
            extra={"flag": key, "error": str(exc)[:200]},
        )
        return None

    if row is None:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        # Tolerate raw scalar text in value_json for hand-edited rows.
        return row[0]


def get_flag(key: str, default: Any = None) -> Any:
    """Resolve a flag using the documented priority order.

    ``key`` is the lowercase Settings attribute name (e.g.
    ``"validation_kernel_mode"``). The matching env var is its uppercase form.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

    settings_default = _settings_value(key)
    env_explicit = _env_explicit(key)

    # 1) For emergency flags: env always wins, even if not explicitly set.
    if key in _ENV_AUTHORITATIVE_FLAGS:
        value = settings_default if settings_default is not None else default
        with _cache_lock:
            _cache[key] = (now, value)
        return value

    # 2) For all other flags: env wins ONLY if explicitly set.
    if env_explicit:
        value = settings_default
        with _cache_lock:
            _cache[key] = (now, value)
        return value

    # 3) DB row.
    db_value = _read_db(key)
    if db_value is not None:
        with _cache_lock:
            _cache[key] = (now, db_value)
        return db_value

    # 4) Settings default → caller default → None.
    value = settings_default if settings_default is not None else default
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def get_bool(key: str, default: bool = False) -> bool:
    value = get_flag(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def get_mode(key: str, default: str = "off") -> str:
    """Resolve a tri-state kernel flag: 'off' | 'shadow' | 'enforce'.

    Unknown values are coerced to ``default`` to fail safe.
    """
    value = get_flag(key, default)
    if not isinstance(value, str):
        return default
    normalised = value.strip().lower()
    if normalised not in {"off", "shadow", "enforce"}:
        return default
    return normalised


def set_db_flag(
    key: str,
    value: Any,
    *,
    description: str | None = None,
    updated_by: str | None = None,
) -> None:
    """Upsert a flag value in the DB. Invalidates the in-process cache.

    Note: this does NOT bypass the .env override — if the operator has set
    the env var (or for the two emergency flags, regardless), the env value
    will continue to win at read time.
    """
    from sqlalchemy import text
    from app.db.session import SessionLocal

    payload = json.dumps(value)
    with SessionLocal() as db:
        # Portable upsert: try update first, fall back to insert.
        result = db.execute(
            text(
                """
                UPDATE feature_flags
                   SET value_json = :v,
                       description = COALESCE(:d, description),
                       updated_by = :u,
                       updated_at = NOW()
                 WHERE key = :k
                """
            ),
            {"k": key, "v": payload, "d": description, "u": updated_by},
        )
        if result.rowcount == 0:
            db.execute(
                text(
                    """
                    INSERT INTO feature_flags (key, value_json, description, updated_by)
                    VALUES (:k, :v, :d, :u)
                    """
                ),
                {"k": key, "v": payload, "d": description, "u": updated_by},
            )
        db.commit()

    invalidate_cache(key)


def invalidate_cache(key: str | None = None) -> None:
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


# --- Convenience getters used by /health and admin views -----------------

_KERNEL_KEYS = (
    "validation_kernel_mode",
    "intent_verifier_mode",
    "safety_kernel_mode",
    "output_defence_mode",
    "loop_detector_mode",
)

_BOOL_FLAG_KEYS = (
    "safe_mode_enabled",
    "strict_mode_enabled",
    "universal_api_enabled",
    "mcp_enabled",
    "dynamic_tools_enabled",
    "dynamic_tools_allow_fallback",
    "permission_v2_enabled",
    "planner_guardrails_enabled",
    "openclaw_persona_enabled",
    "hitl_enabled",
    "dry_run_enabled",
    "risk_registry_enabled",
    "action_summariser_enabled",
    "allow_http_tools",
)


def snapshot() -> dict[str, Any]:
    """Return a shallow snapshot of all v4 flags for /health and admin UI."""
    kernels = {k: get_mode(k) for k in _KERNEL_KEYS}
    flags = {k: get_bool(k) for k in _BOOL_FLAG_KEYS}
    return {"kernels": kernels, "flags": flags}
