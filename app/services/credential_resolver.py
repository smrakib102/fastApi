from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, Set

from app.core.config import settings
from app.db.session import SessionLocal
from app.api.routes.google_oauth import _ensure_token, _get_default_account
from app.services.scope_registry import map_required_scopes, normalize_scope_list
from app.models.agent_credential import AgentCredential
from app.models.oauth_credential import OAuthCredential
from app.services.oauth_metrics import (
    record_refresh_result,
    record_reconnect_trigger,
    record_scope_block_event,
)

logger = logging.getLogger("credential_resolver")


@dataclass(frozen=True)
class CredentialContext:
    user_id: int
    agent_id: Optional[int]
    tool_name: str
    execution_id: str
    retry_count: int
    previous_error: Optional[str] = None


@dataclass(frozen=True)
class CredentialTrace:
    selection_reason: str
    fallback_triggered: bool
    scope_check_result: str


@dataclass(frozen=True)
class CredentialResult:
    access_token: Optional[str]
    refresh_token: Optional[str]
    source: str
    scopes: Set[str]
    status: str
    credential_id: Optional[int]
    trace: CredentialTrace = field(default_factory=lambda: CredentialTrace(
        selection_reason="unresolved",
        fallback_triggered=False,
        scope_check_result="not_checked",
    ))


def resolve_credential(context: CredentialContext) -> CredentialResult:
    db = SessionLocal()
    try:
        allowlisted = _is_allowlisted(context.user_id)
        logger.info(
            "allowlist_decision",
            extra={
                "user_id": context.user_id,
                "agent_id": context.agent_id,
                "tool_name": context.tool_name,
                "allowlisted": allowlisted,
            },
        )

        selection_reason = "legacy_fallback"
        fallback_triggered = True
        source = "legacy"
        access_token = None
        refresh_token = None
        credential_id = None
        scope_raw = None

        enforcement_active = allowlisted and settings.vault_scope_enforcement_enabled

        if allowlisted and settings.vault_execution_enabled:
            vault_credential, vault_reason = _select_vault_credential(db, context)
            if vault_credential and vault_credential.access_token:
                if vault_credential.invalid_state:
                    _trigger_reconnect(db, context, "invalid_state")
                    record_reconnect_trigger()
                    if settings.legacy_fallback_enabled:
                        vault_credential = None
                        vault_reason = "vault_invalid_state"
                    else:
                        return CredentialResult(
                            access_token=None,
                            refresh_token=None,
                            source="vault",
                            scopes=set(),
                            status="needs_reauth",
                            credential_id=vault_credential.id,
                            trace=CredentialTrace(
                                selection_reason="vault_invalid_state",
                                fallback_triggered=True,
                                scope_check_result="not_checked",
                            ),
                        )

            if vault_credential and vault_credential.access_token:
                selection_reason = vault_reason
                fallback_triggered = False
                source = "vault"
                credential_id = vault_credential.id
                scope_raw = vault_credential.scope
                vault_access_token = vault_credential.access_token
                vault_refresh_token = vault_credential.refresh_token
                if _should_refresh_vault(vault_credential, context):
                    refreshed = _refresh_vault_credential(db, vault_credential, context)
                    if refreshed:
                        vault_credential = refreshed
                        vault_access_token = refreshed.access_token
                        vault_refresh_token = refreshed.refresh_token
                access_token = vault_access_token
                refresh_token = vault_refresh_token

        if source != "vault":
            if not settings.legacy_fallback_enabled:
                return CredentialResult(
                    access_token=None,
                    refresh_token=None,
                    source="legacy",
                    scopes=set(),
                    status="missing",
                    credential_id=None,
                    trace=CredentialTrace(
                        selection_reason="legacy_disabled",
                        fallback_triggered=True,
                        scope_check_result="not_checked",
                    ),
                )
            account = _ensure_token(db, _get_default_account(db, context.user_id))
            access_token = account.access_token
            refresh_token = account.refresh_token
            credential_id = account.id
            scope_raw = account.scope
    except Exception:
        return CredentialResult(
            access_token=None,
            refresh_token=None,
            source="legacy",
            scopes=set(),
            status="missing",
            credential_id=None,
            trace=CredentialTrace(
                selection_reason="legacy_missing",
                fallback_triggered=True,
                scope_check_result="not_checked",
            ),
        )
    finally:
        db.close()

    if source == "legacy":
        _maybe_refresh_legacy_from_scope(scope_raw, context)

    logger.info(
        "vault_vs_legacy_selection",
        extra={
            "user_id": context.user_id,
            "agent_id": context.agent_id,
            "tool_name": context.tool_name,
            "selected_source": source,
            "selection_reason": selection_reason,
        },
    )
    logger.info(
        "credential_source",
        extra={
            "user_id": context.user_id,
            "agent_id": context.agent_id,
            "tool_name": context.tool_name,
            "credential_source": source,
        },
    )

    required_scopes: Set[str] = set()
    granted_scopes = normalize_scope_list(scope_raw)
    scope_check_result = "not_checked"
    match_score = 1.0
    if enforcement_active:
        try:
            from app.plugins import plugin_registry

            plugin_registry.discover()
            plugin = plugin_registry.get(context.tool_name)
            if plugin is not None:
                required_scopes = map_required_scopes("google", plugin.required_scopes)
        except Exception:
            required_scopes = set()

        if required_scopes:
            matched = required_scopes.intersection(granted_scopes)
            match_score = len(matched) / len(required_scopes)
            scope_check_result = "pass" if match_score >= 1.0 else "fail"

        if scope_check_result == "fail":
            record_scope_block_event()
            _trigger_reconnect(db, context, "scope_mismatch")
            record_reconnect_trigger()

        logger.info(
            "credential_scope_check",
            extra={
                "tool_name": context.tool_name,
                "user_id": context.user_id,
                "agent_id": context.agent_id,
                "required_scopes": sorted(required_scopes),
                "granted_scopes": sorted(granted_scopes),
                "match_score": match_score,
            },
        )

    status = "valid"
    if scope_check_result == "fail":
        status = "needs_reauth"

    return CredentialResult(
        access_token=access_token,
        refresh_token=refresh_token,
        source=source,
        scopes=granted_scopes,
        status=status,
        credential_id=credential_id,
        trace=CredentialTrace(
            selection_reason=selection_reason,
            fallback_triggered=fallback_triggered,
            scope_check_result=scope_check_result,
        ),
    )


def _should_refresh_vault(credential: OAuthCredential, context: CredentialContext) -> bool:
    if not settings.vault_refresh_enabled:
        return False
    if context.retry_count >= 1:
        return False
    if not credential.refresh_token:
        return False
    if context.previous_error and "401" in context.previous_error:
        return True
    if not credential.expires_at:
        return False
    now = datetime.now(timezone.utc)
    return credential.expires_at <= now + timedelta(seconds=120)


def _refresh_vault_credential(
    db,
    credential: OAuthCredential,
    context: CredentialContext,
) -> OAuthCredential | None:
    lock_key = f"oauth:refresh:credential:{credential.id}"
    lock_acquired = False
    try:
        from app.core.redis_client import get_redis

        lock_acquired = bool(get_redis().set(lock_key, "1", nx=True, ex=60))
    except Exception:
        lock_acquired = True

    if not lock_acquired:
        logger.info(
            "credential_refresh_lock_skipped",
            extra={"credential_id": credential.id, "execution_id": context.execution_id},
        )
        return None

    logger.info(
        "credential_refresh_attempted",
        extra={"credential_id": credential.id, "execution_id": context.execution_id},
    )

    try:
        import httpx

        payload = {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token,
        }
        response = httpx.post("https://oauth2.googleapis.com/token", data=payload, timeout=30)
        if response.status_code != 200:
            logger.info(
                "credential_refresh_failed",
                extra={
                    "credential_id": credential.id,
                    "execution_id": context.execution_id,
                    "status": response.status_code,
                },
            )
            record_refresh_result(False)
            return None

        data = response.json()
        new_access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not new_access_token:
            logger.info(
                "credential_refresh_failed",
                extra={
                    "credential_id": credential.id,
                    "execution_id": context.execution_id,
                    "reason": "missing_access_token",
                },
            )
            record_refresh_result(False)
            return None

        new_expires_at = None
        if expires_in:
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

        updated = (
            db.query(OAuthCredential)
            .filter(
                OAuthCredential.id == credential.id,
                OAuthCredential.updated_at == credential.updated_at,
            )
            .update(
                {
                    "access_token": new_access_token,
                    "expires_at": new_expires_at,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )
        if not updated:
            logger.info(
                "credential_refresh_failed",
                extra={
                    "credential_id": credential.id,
                    "execution_id": context.execution_id,
                    "reason": "cas_failed",
                },
            )
            db.rollback()
            record_refresh_result(False)
            return None

        db.commit()
        db.refresh(credential)
        logger.info(
            "credential_refresh_success",
            extra={"credential_id": credential.id, "execution_id": context.execution_id},
        )
        record_refresh_result(True)
        return credential
    except Exception:
        logger.info(
            "credential_refresh_failed",
            extra={"credential_id": credential.id, "execution_id": context.execution_id},
        )
        record_refresh_result(False)
        db.rollback()
        return None
    finally:
        if lock_acquired:
            try:
                from app.core.redis_client import get_redis

                get_redis().delete(lock_key)
            except Exception:
                pass


def _maybe_refresh_legacy_from_scope(scope_raw: str | None, context: CredentialContext) -> None:
    if not settings.vault_refresh_enabled:
        return
    if context.retry_count >= 1:
        return

    # Shadow-only: legacy refresh remains a no-op in this phase.

    lock_key = f"oauth:refresh:{context.user_id}"
    lock_acquired = False
    try:
        from app.core.redis_client import get_redis

        lock_acquired = bool(get_redis().set(lock_key, "1", nx=True, ex=60))
    except Exception:
        lock_acquired = True

    if not lock_acquired:
        logger.info(
            "credential_refresh_lock_skipped",
            extra={"user_id": context.user_id, "execution_id": context.execution_id},
        )
        return

    logger.info(
        "credential_refresh_attempted",
        extra={"user_id": context.user_id, "execution_id": context.execution_id},
    )

    try:
        # Shadow-only: placeholder for refresh + CAS update.
        logger.info(
            "credential_refresh_failed",
            extra={
                "user_id": context.user_id,
                "execution_id": context.execution_id,
                "reason": "refresh_not_implemented",
            },
        )
    finally:
        if lock_acquired:
            try:
                from app.core.redis_client import get_redis

                get_redis().delete(lock_key)
            except Exception:
                pass


def _is_allowlisted(user_id: int) -> bool:
    if not settings.vault_execution_enabled:
        return False
    raw = settings.oauth_allowlist_user_ids
    if not raw:
        return False
    allowlist: Set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            allowlist.add(int(token))
        except ValueError:
            continue
    return user_id in allowlist


def _select_vault_credential(
    db,
    context: CredentialContext,
) -> tuple[OAuthCredential | None, str]:
    if context.agent_id is not None:
        row = (
            db.query(AgentCredential, OAuthCredential)
            .join(OAuthCredential, OAuthCredential.id == AgentCredential.credential_id)
            .filter(AgentCredential.agent_id == context.agent_id)
            .order_by(AgentCredential.created_at.desc())
            .first()
        )
        if row:
            return row[1], "vault_agent_linked"

    credential = (
        db.query(OAuthCredential)
        .filter(OAuthCredential.user_id == context.user_id)
        .order_by(OAuthCredential.updated_at.desc())
        .first()
    )
    if credential:
        return credential, "vault_user"
    return None, "vault_missing"


def _trigger_reconnect(db, context: CredentialContext, reason: str) -> None:
    try:
        from app.models.user import User
        from app.services.permission_service import permission_service

        user = db.get(User, context.user_id)
        if not user:
            return
        permission_service.request(
            db,
            user=user,
            tool_name=context.tool_name,
            reason=f"{context.tool_name} requires reconnect: {reason}",
        )
        db.flush()
    except Exception:
        logger.info(
            "reconnect_trigger_failed",
            extra={
                "user_id": context.user_id,
                "agent_id": context.agent_id,
                "tool_name": context.tool_name,
                "reason": reason,
            },
        )


__all__ = [
    "CredentialContext",
    "CredentialResult",
    "CredentialTrace",
    "resolve_credential",
]
