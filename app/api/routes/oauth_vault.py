from typing import Any
from datetime import datetime, timezone
import hashlib
import hmac
import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.services.audit_log import record_audit
from app.models.user import User
from app.services.oauth_audit import log_event
from app.services.oauth_contract import get_oauth_error_code, get_oauth_request_id_regex
from app.services.oauth_metrics import (
    record_anomaly,
    record_callback,
    record_duplicate_attempt,
    record_invalid_state_rejected,
    record_unknown_oauth_request_id,
    record_vault_failure,
    record_vault_write_latency,
)
from app.services.oauth_request_store import (
    acquire_processing_lock,
    get_processed_result,
    get_request,
    set_processed_result,
)
from app.services.oauth_vault import (
    ensure_agent_credential,
    evaluate_credential_state,
    upsert_oauth_credential,
)
from app.services.permission_service import permission_service

router = APIRouter()


class OAuthVaultIngestPayload(BaseModel):
    oauth_request_id: str
    provider: str
    provider_account_id: str
    account_email: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    scope: Any | None = None
    expires_at: Any | None = None


_PROVIDER_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]{3,255}$")
_OAUTH_REQUEST_ID_RE = re.compile(get_oauth_request_id_regex())


def _validate_provider_account_id(provider_account_id: str) -> None:
    if not _PROVIDER_ACCOUNT_RE.match(provider_account_id):
        record_anomaly("callback_invalid_provider_account_id")
        raise HTTPException(status_code=400, detail="Invalid provider account id")


def _validate_oauth_request_id(oauth_request_id: str) -> None:
    if not oauth_request_id or not _OAUTH_REQUEST_ID_RE.match(oauth_request_id):
        record_invalid_state_rejected()
        log_event(get_oauth_error_code("invalid"), request_id=oauth_request_id)
        raise HTTPException(status_code=400, detail=get_oauth_error_code("invalid"))


def _validate_signature(
    body: bytes,
    timestamp_raw: str | None,
    signature_raw: str | None,
    request_id: str | None,
) -> None:
    if not settings.nextauth_signature_secret:
        raise HTTPException(status_code=500, detail="NextAuth signature secret is not configured")
    if not timestamp_raw or not signature_raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature headers")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid timestamp") from exc

    now = int(time.time())
    drift = abs(now - timestamp)
    if drift > settings.oauth_callback_max_skew_seconds:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature timestamp expired")
    if drift > settings.oauth_callback_drift_log_seconds:
        log_event(
            "callback_timestamp_drift",
            request_id=request_id,
            drift_seconds=drift,
        )
        record_anomaly("callback_timestamp_drift")

    message = f"{timestamp}.".encode("utf-8") + body
    expected = hmac.new(
        settings.nextauth_signature_secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(expected, signature_raw):
        return

    secondary = settings.nextauth_signature_secondary_secret
    if secondary:
        secondary_expected = hmac.new(
            secondary.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(secondary_expected, signature_raw):
            log_event("callback_signature_secondary", request_id=request_id)
            return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


@router.post("/callback")
async def oauth_vault_callback(
    payload: OAuthVaultIngestPayload,
    request: Request,
    db: Session = Depends(get_db),
    x_nextauth_secret: str | None = Header(default=None, alias="X-NextAuth-Secret"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
):
    start_time = time.monotonic()
    try:
        if not settings.enable_vault_system:
            raise HTTPException(status_code=403, detail="Vault system is disabled")
        if not settings.nextauth_callback_secret:
            raise HTTPException(status_code=500, detail="NextAuth callback secret is not configured")
        if x_nextauth_secret != settings.nextauth_callback_secret:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

        raw_body = await request.body()
        _validate_signature(raw_body, x_timestamp, x_signature, payload.oauth_request_id)
        _validate_oauth_request_id(payload.oauth_request_id)
        _validate_provider_account_id(payload.provider_account_id)

        if payload.provider != "google":
            record_anomaly("callback_provider_mismatch")
            raise HTTPException(status_code=400, detail="Unsupported provider")

        request_payload = get_request(payload.oauth_request_id)
        if not request_payload:
            record_anomaly("callback_missing_request")
            record_unknown_oauth_request_id()
            log_event(get_oauth_error_code("unknown"), request_id=payload.oauth_request_id)
            raise HTTPException(status_code=400, detail=get_oauth_error_code("unknown"))

        if request_payload.get("provider") != payload.provider:
            record_anomaly("callback_provider_mismatch")
            raise HTTPException(status_code=400, detail="OAuth request provider mismatch")

        processed = get_processed_result(payload.oauth_request_id)
        if processed:
            record_duplicate_attempt(payload.oauth_request_id)
            log_event(
                "vault_callback_replay",
                request_id=payload.oauth_request_id,
                user_id=request_payload.get("user_id"),
            )
            return processed

        if not acquire_processing_lock(payload.oauth_request_id):
            record_duplicate_attempt(payload.oauth_request_id)
            raise HTTPException(status_code=409, detail="OAuth request is already processing")

        if request_payload.get("route_mode") not in {"nextauth", None}:
            record_anomaly("callback_route_mismatch")
            raise HTTPException(status_code=400, detail="OAuth request is not eligible for vault ingest")

        invalid_state, invalid_reason = evaluate_credential_state(
            refresh_token=payload.refresh_token,
            scope=payload.scope,
            expires_at=payload.expires_at,
        )
        invalid_at = datetime.now(timezone.utc) if invalid_state else None

        try:
            with db.begin():
                credential, created = upsert_oauth_credential(
                    db,
                    user_id=int(request_payload["user_id"]),
                    provider=payload.provider,
                    provider_account_id=payload.provider_account_id,
                    account_email=payload.account_email,
                    access_token=payload.access_token,
                    refresh_token=payload.refresh_token,
                    token_type=payload.token_type,
                    scope=payload.scope,
                    expires_at=payload.expires_at,
                    invalid_state=invalid_state,
                    invalid_reason=invalid_reason,
                    invalid_at=invalid_at,
                )

                agent_credential = ensure_agent_credential(
                    db,
                    agent_id=request_payload.get("agent_id"),
                    credential_id=credential.id,
                    required_scopes=request_payload.get("required_scopes"),
                )

                if invalid_state:
                    record_anomaly("vault_invalid_state")
                    tool_names = request_payload.get("tool_names") or []
                    user = db.get(User, int(request_payload["user_id"]))
                    if user and tool_names:
                        permission_service.request_many(
                            db,
                            user=user,
                            tool_names=tool_names,
                            reason="OAuth credential invalid_state; re-auth required.",
                        )

                record_audit(
                    db,
                    user_id=int(request_payload["user_id"]),
                    action="oauth_vault_ingest",
                    resource_type="oauth_credentials",
                    resource_id=str(credential.id),
                    metadata={
                        "provider": payload.provider,
                        "agent_id": request_payload.get("agent_id"),
                        "request_id": payload.oauth_request_id,
                        "created": created,
                        "invalid_state": invalid_state,
                        "invalid_reason": invalid_reason,
                    },
                )
        except Exception as exc:
            record_vault_failure()
            log_event("vault_callback_failed", request_id=payload.oauth_request_id, error=str(exc))
            raise

        latency_ms = (time.monotonic() - start_time) * 1000.0
        record_callback(True, latency_ms)
        record_vault_write_latency(latency_ms)

        status_label = "invalid_state" if invalid_state else "stored"
        result = {
            "status": status_label,
            "credential_id": credential.id,
            "agent_credential_id": getattr(agent_credential, "id", None),
            "invalid_reason": invalid_reason,
        }
        set_processed_result(payload.oauth_request_id, result)

        log_event(
            "vault_callback_processed",
            request_id=payload.oauth_request_id,
            user_id=request_payload.get("user_id"),
            provider=payload.provider,
            credential_id=credential.id,
            agent_credential_id=getattr(agent_credential, "id", None),
            status=status_label,
            latency_ms=latency_ms,
            created=created,
            invalid_state=invalid_state,
        )

        return result
    except HTTPException as exc:
        latency_ms = (time.monotonic() - start_time) * 1000.0
        record_callback(False, latency_ms)
        log_event(
            "vault_callback_rejected",
            request_id=payload.oauth_request_id,
            status_code=exc.status_code,
            detail=str(exc.detail),
        )
        raise
    except Exception as exc:
        latency_ms = (time.monotonic() - start_time) * 1000.0
        record_callback(False, latency_ms)
        log_event("vault_callback_error", request_id=payload.oauth_request_id, error=str(exc))
        raise
