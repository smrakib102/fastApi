import time
from typing import Any

from app.core.config import settings
from app.services.oauth_contract import get_oauth_metric
from app.core.redis_client import get_redis
from app.services.oauth_audit import log_event


KILL_SWITCH_KEY = "oauth:kill_switch"


def _window_key(metric: str, window: int) -> str:
    return f"oauth:metrics:{metric}:{window}"


def _current_window() -> int:
    return int(time.time() // settings.oauth_metrics_window_seconds)


def _increment_count(metric: str, value: int = 1) -> int:
    redis_client = get_redis()
    window = _current_window()
    key = _window_key(metric, window)
    new_value = redis_client.incr(key, value)
    redis_client.expire(key, settings.oauth_metrics_window_seconds * 2)
    return int(new_value)


def _increment_sum(metric: str, value: float) -> None:
    redis_client = get_redis()
    window = _current_window()
    key = _window_key(metric, window)
    redis_client.incrbyfloat(key, value)
    redis_client.expire(key, settings.oauth_metrics_window_seconds * 2)


def _set_kill_switch(reason: str, count: int, threshold: int) -> None:
    redis_client = get_redis()
    if redis_client.set(KILL_SWITCH_KEY, reason, nx=True, ex=settings.oauth_kill_switch_ttl_seconds):
        log_event("oauth_kill_switch_enabled", reason=reason, count=count, threshold=threshold)


def is_kill_switch_enabled() -> bool:
    redis_client = get_redis()
    return bool(redis_client.get(KILL_SWITCH_KEY))


def _maybe_trip_kill_switch(metric: str, count: int, threshold: int) -> None:
    if threshold > 0 and count >= threshold:
        _set_kill_switch(metric, count, threshold)


def record_callback(success: bool, latency_ms: float) -> None:
    count = _increment_count("callback_success" if success else "callback_failure")
    _increment_sum("callback_latency_sum_ms", latency_ms)
    _increment_count("callback_latency_count")
    if not success:
        _maybe_trip_kill_switch("callback_failure", count, settings.oauth_callback_failure_threshold)


def record_vault_failure() -> None:
    count = _increment_count("vault_failure")
    _maybe_trip_kill_switch("vault_failure", count, settings.oauth_vault_failure_threshold)


def record_duplicate_attempt(request_id: str) -> None:
    _increment_count("duplicate_attempt")
    redis_client = get_redis()
    key = f"oauth:metrics:retry:{request_id}"
    redis_client.incr(key)
    redis_client.expire(key, settings.oauth_metrics_window_seconds * 2)


def record_anomaly(metric: str) -> None:
    count = _increment_count(metric)
    _maybe_trip_kill_switch(metric, count, settings.oauth_duplicate_threshold)


def record_invalid_state_rejected() -> None:
    _increment_count(get_oauth_metric("invalid_state_rejected"))


def record_unknown_oauth_request_id() -> None:
    _increment_count(get_oauth_metric("unknown_oauth_request_id"))


def record_vault_write_latency(latency_ms: float) -> None:
    _increment_sum("vault_latency_sum_ms", latency_ms)
    _increment_count("vault_latency_count")


def get_metrics_snapshot() -> dict[str, Any]:
    redis_client = get_redis()
    window = _current_window()
    keys = {
        "callback_success": _window_key("callback_success", window),
        "callback_failure": _window_key("callback_failure", window),
        "invalid_state_rejected": _window_key(get_oauth_metric("invalid_state_rejected"), window),
        "unknown_oauth_request_id": _window_key(get_oauth_metric("unknown_oauth_request_id"), window),
        "duplicate_attempt": _window_key("duplicate_attempt", window),
        "callback_latency_sum_ms": _window_key("callback_latency_sum_ms", window),
        "callback_latency_count": _window_key("callback_latency_count", window),
        "vault_failure": _window_key("vault_failure", window),
        "vault_latency_sum_ms": _window_key("vault_latency_sum_ms", window),
        "vault_latency_count": _window_key("vault_latency_count", window),
    }
    raw = redis_client.mget(list(keys.values()))
    data = dict(zip(keys.keys(), raw, strict=False))

    def _to_int(value: str | None) -> int:
        try:
            return int(float(value)) if value is not None else 0
        except ValueError:
            return 0

    def _to_float(value: str | None) -> float:
        try:
            return float(value) if value is not None else 0.0
        except ValueError:
            return 0.0

    callback_success = _to_int(data["callback_success"])
    callback_failure = _to_int(data["callback_failure"])
    invalid_state_rejected = _to_int(data["invalid_state_rejected"])
    unknown_oauth_request_id = _to_int(data["unknown_oauth_request_id"])
    duplicate_attempt = _to_int(data["duplicate_attempt"])
    callback_latency_sum = _to_float(data["callback_latency_sum_ms"])
    callback_latency_count = max(1, _to_int(data["callback_latency_count"]))
    vault_failure = _to_int(data["vault_failure"])
    vault_latency_sum = _to_float(data["vault_latency_sum_ms"])
    vault_latency_count = max(1, _to_int(data["vault_latency_count"]))

    total_callbacks = callback_success + callback_failure
    return {
        "window_seconds": settings.oauth_metrics_window_seconds,
        "callback_success": callback_success,
        "callback_failure": callback_failure,
        "invalid_state_rejected": invalid_state_rejected,
        "unknown_oauth_request_id": unknown_oauth_request_id,
        "callback_success_rate": (callback_success / total_callbacks) if total_callbacks else 0.0,
        "duplicate_attempt": duplicate_attempt,
        "duplicate_attempt_rate": (duplicate_attempt / total_callbacks) if total_callbacks else 0.0,
        "callback_latency_avg_ms": callback_latency_sum / callback_latency_count,
        "vault_failure": vault_failure,
        "vault_latency_avg_ms": vault_latency_sum / vault_latency_count,
        "kill_switch_enabled": is_kill_switch_enabled(),
    }


def get_retry_count(request_id: str) -> int:
    redis_client = get_redis()
    raw = redis_client.get(f"oauth:metrics:retry:{request_id}")
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0
