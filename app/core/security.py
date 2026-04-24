from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jose import jwt
from passlib.context import CryptContext
import redis

from app.core.config import settings
from app.core.redis_client import get_redis

pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(password, hashed_password)
    except ValueError:
        return False


def _register_session(jti: str, user_id: str, expires_at: datetime) -> None:
    ttl_seconds = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    if ttl_seconds <= 0:
        return
    redis_client = get_redis()
    try:
        redis_client.setex(f"auth:session:{jti}", ttl_seconds, "1")
        redis_client.sadd(f"auth:user_sessions:{user_id}", jti)
        redis_client.expire(f"auth:user_sessions:{user_id}", ttl_seconds)
    except redis.RedisError as exc:
        raise RuntimeError("Redis unavailable") from exc


def revoke_session(jti: str, user_id: str | None = None) -> None:
    redis_client = get_redis()
    try:
        redis_client.delete(f"auth:session:{jti}")
        if user_id:
            redis_client.srem(f"auth:user_sessions:{user_id}", jti)
    except redis.RedisError as exc:
        raise RuntimeError("Redis unavailable") from exc


def revoke_user_sessions(user_id: str) -> None:
    redis_client = get_redis()
    key = f"auth:user_sessions:{user_id}"
    try:
        jtis = redis_client.smembers(key)
        if jtis:
            redis_client.delete(*[f"auth:session:{jti}" for jti in jtis])
        redis_client.delete(key)
    except redis.RedisError as exc:
        raise RuntimeError("Redis unavailable") from exc


def is_session_active(jti: str) -> bool:
    try:
        return bool(get_redis().get(f"auth:session:{jti}"))
    except redis.RedisError as exc:
        raise RuntimeError("Redis unavailable") from exc


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    expire_minutes = expires_minutes or settings.auth_access_token_minutes
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    jti = uuid4().hex
    payload = {
        "sub": subject,
        "exp": expires_at,
        "iss": settings.auth_token_issuer,
        "iat": datetime.now(timezone.utc),
        "jti": jti,
        "typ": "access",
    }
    token = jwt.encode(payload, settings.auth_secret_key, algorithm=settings.auth_algorithm)
    _register_session(jti, subject, expires_at)
    return token


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.auth_secret_key,
        algorithms=[settings.auth_algorithm],
        issuer=settings.auth_token_issuer,
    )
