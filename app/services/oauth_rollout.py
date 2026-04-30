import hashlib
import json
from dataclasses import dataclass

from app.core.config import settings
from app.core.redis_client import get_redis
from app.services.oauth_metrics import is_kill_switch_enabled


@dataclass(frozen=True)
class RouteDecision:
    mode: str
    bucket: int | None
    source: str
    cached: bool


def _parse_allowlist(raw: str | None) -> set[int]:
    if not raw:
        return set()
    items = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            items.append(int(token))
        except ValueError:
            continue
    return set(items)


def _hash_bucket(user_id: int) -> int:
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _route_cache_key(user_id: int) -> str:
    return f"oauth:route:{user_id}"


def _get_cached_route(user_id: int) -> RouteDecision | None:
    redis_client = get_redis()
    raw = redis_client.get(_route_cache_key(user_id))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return RouteDecision(
        mode=payload.get("mode", "legacy"),
        bucket=payload.get("bucket"),
        source=payload.get("source", "cache"),
        cached=True,
    )


def _set_cached_route(user_id: int, decision: RouteDecision) -> None:
    redis_client = get_redis()
    redis_client.setex(
        _route_cache_key(user_id),
        settings.oauth_route_ttl_seconds,
        json.dumps(
            {
                "mode": decision.mode,
                "bucket": decision.bucket,
                "source": decision.source,
            },
            ensure_ascii=True,
        ),
    )


def get_route_decision(user_id: int) -> RouteDecision:
    if is_kill_switch_enabled():
        return RouteDecision(mode="legacy", bucket=None, source="kill_switch", cached=False)

    cached = _get_cached_route(user_id)
    if cached:
        return cached

    allowlist = _parse_allowlist(settings.oauth_allowlist_user_ids)
    mode = settings.oauth_rollout_mode.lower()
    if mode == "allowlist":
        decision = RouteDecision(
            mode="nextauth" if user_id in allowlist else "legacy",
            bucket=None,
            source="allowlist",
            cached=False,
        )
        _set_cached_route(user_id, decision)
        return decision

    percent = max(0, min(100, settings.oauth_rollout_percent))
    bucket = _hash_bucket(user_id)
    decision = RouteDecision(
        mode="nextauth" if bucket < percent else "legacy",
        bucket=bucket,
        source="hash",
        cached=False,
    )
    _set_cached_route(user_id, decision)
    return decision
