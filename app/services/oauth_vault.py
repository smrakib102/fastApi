from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.agent_credential import AgentCredential
from app.models.oauth_credential import OAuthCredential


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return _parse_expires_at(int(raw))
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _normalize_scope(scope: Any) -> str | None:
    if scope is None:
        return None
    if isinstance(scope, list):
        return " ".join([str(item) for item in scope if item]) or None
    if isinstance(scope, str):
        return scope.strip() or None
    return None


def evaluate_credential_state(
    *,
    refresh_token: str | None,
    scope: Any,
    expires_at: Any,
) -> tuple[bool, str | None]:
    reasons: list[str] = []
    if not refresh_token:
        reasons.append("missing_refresh_token")

    normalized_scope = _normalize_scope(scope)
    if not normalized_scope:
        reasons.append("corrupted_scope")

    parsed_expires_at = _parse_expires_at(expires_at)
    if parsed_expires_at and parsed_expires_at <= datetime.now(timezone.utc):
        reasons.append("expired_token")

    if reasons:
        return True, ",".join(reasons)
    return False, None


def upsert_oauth_credential(
    db: Session,
    *,
    user_id: int,
    provider: str,
    provider_account_id: str,
    account_email: str | None,
    access_token: str | None,
    refresh_token: str | None,
    token_type: str | None,
    scope: Any,
    expires_at: Any,
    invalid_state: bool,
    invalid_reason: str | None,
    invalid_at: datetime | None,
) -> tuple[OAuthCredential, bool]:
    now = datetime.now(timezone.utc)
    stmt = (
        insert(OAuthCredential)
        .values(
            user_id=user_id,
            provider=provider,
            provider_account_id=provider_account_id,
            account_email=account_email,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            scope=_normalize_scope(scope),
            expires_at=_parse_expires_at(expires_at),
            invalid_state=invalid_state,
            invalid_reason=invalid_reason,
            invalid_at=invalid_at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "provider", "provider_account_id"],
            set_={
                "account_email": account_email,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": token_type,
                "scope": _normalize_scope(scope),
                "expires_at": _parse_expires_at(expires_at),
                "invalid_state": invalid_state,
                "invalid_reason": invalid_reason,
                "invalid_at": invalid_at,
                "updated_at": now,
            },
        )
        .returning(OAuthCredential.id, OAuthCredential.created_at)
    )
    result = db.execute(stmt)
    row = result.one()
    credential_id = row[0]
    created_at = row[1]
    credential = db.get(OAuthCredential, credential_id)
    created = False
    if created_at and credential:
        created = abs((created_at - now).total_seconds()) < 1
    return credential, created


def ensure_agent_credential(
    db: Session,
    *,
    agent_id: int | None,
    credential_id: int,
    required_scopes: list[str] | None,
) -> AgentCredential | None:
    if agent_id is None:
        return None
    scopes_text = " ".join(required_scopes or []) or None
    stmt = (
        insert(AgentCredential)
        .values(
            agent_id=agent_id,
            credential_id=credential_id,
            required_scopes=scopes_text,
        )
        .on_conflict_do_nothing(
            index_elements=["agent_id", "credential_id"],
        )
        .returning(AgentCredential.id)
    )
    result = db.execute(stmt)
    agent_credential_id = result.scalar_one_or_none()
    if agent_credential_id is None:
        return (
            db.query(AgentCredential)
            .filter(
                AgentCredential.agent_id == agent_id,
                AgentCredential.credential_id == credential_id,
            )
            .one_or_none()
        )
    return db.get(AgentCredential, agent_credential_id)
