import json
import secrets
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.redis_client import get_redis


REQUEST_KEY_PREFIX = "oauth:req:"
PROCESSED_KEY_PREFIX = "oauth:req:processed:"
LOCK_KEY_PREFIX = "oauth:req:lock:"
RESULT_KEY_PREFIX = "oauth:req:result:"


def create_request(
    user_id: int,
    agent_id: int | None,
    tool_names: list[str] | None,
    required_scopes: list[str] | None,
    route_mode: str | None = None,
    route_bucket: int | None = None,
    routing_source: str | None = None,
    provider: str = "google",
) -> str:
    request_id = secrets.token_urlsafe(32)
    payload = {
        "request_id": request_id,
        "provider": provider,
        "user_id": user_id,
        "agent_id": agent_id,
        "tool_names": tool_names or [],
        "required_scopes": required_scopes or [],
        "route_mode": route_mode,
        "route_bucket": route_bucket,
        "routing_source": routing_source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    redis_client = get_redis()
    redis_client.setex(
        f"{REQUEST_KEY_PREFIX}{request_id}",
        settings.oauth_request_ttl_seconds,
        json.dumps(payload),
    )
    return request_id


def get_request(request_id: str) -> dict[str, Any] | None:
    redis_client = get_redis()
    raw = redis_client.get(f"{REQUEST_KEY_PREFIX}{request_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def mark_processed(request_id: str) -> bool:
    redis_client = get_redis()
    return bool(
        redis_client.set(
            f"{PROCESSED_KEY_PREFIX}{request_id}",
            "1",
            nx=True,
            ex=settings.oauth_processed_ttl_seconds,
        )
    )


def acquire_processing_lock(request_id: str) -> bool:
    redis_client = get_redis()
    return bool(
        redis_client.set(
            f"{LOCK_KEY_PREFIX}{request_id}",
            "1",
            nx=True,
            ex=settings.oauth_processing_lock_seconds,
        )
    )


def get_processed_result(request_id: str) -> dict[str, Any] | None:
    redis_client = get_redis()
    raw = redis_client.get(f"{RESULT_KEY_PREFIX}{request_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_processed_result(request_id: str, result: dict[str, Any]) -> None:
    redis_client = get_redis()
    redis_client.setex(
        f"{RESULT_KEY_PREFIX}{request_id}",
        settings.oauth_processed_ttl_seconds,
        json.dumps(result, ensure_ascii=True),
    )
