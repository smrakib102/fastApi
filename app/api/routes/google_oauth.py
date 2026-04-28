from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode

import httpx
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.core.config import settings
from app.core.redis_client import get_redis
from app.models.google_account import GoogleAccount
from app.models.user import User

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


def _disconnect_google_account(db: Session, user_id: int) -> None:
    account = db.execute(
        select(GoogleAccount).where(GoogleAccount.user_id == user_id)
    ).scalar_one_or_none()
    if not account:
        return
    db.delete(account)
    db.commit()


@router.delete("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def google_disconnect(current_user: User = Depends(require_user), db: Session = Depends(get_db)):
    _disconnect_google_account(db, current_user.id)
    return


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def google_disconnect_post(current_user: User = Depends(require_user), db: Session = Depends(get_db)):
    _disconnect_google_account(db, current_user.id)
    return


def _require_google_config() -> None:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")


def _build_scopes() -> str:
    scopes = settings.google_oauth_scopes or ""
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    if not scope_list:
        return "openid email profile"
    return " ".join(scope_list)


def _get_default_account(db: Session, user_id: int) -> GoogleAccount:
    account = db.execute(
        select(GoogleAccount).where(GoogleAccount.user_id == user_id)
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="No Google account connected")
    return account


def _refresh_token(db: Session, account: GoogleAccount) -> GoogleAccount:
    if not account.refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    payload = {
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": account.refresh_token,
    }

    response = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Failed to refresh token")

    data = response.json()
    account.access_token = data["access_token"]
    expires_in = data.get("expires_in")
    if expires_in:
        account.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _ensure_token(db: Session, account: GoogleAccount) -> GoogleAccount:
    if account.expires_at:
        now = datetime.now(timezone.utc)
        if now + timedelta(seconds=60) >= account.expires_at:
            return _refresh_token(db, account)
    return account


@router.get("/oauth/start")
def google_oauth_start(current_user: User = Depends(require_user)):
    _require_google_config()

    state_token = secrets.token_urlsafe(32)
    redis_client = get_redis()
    redis_client.setex(
        f"oauth:state:{state_token}",
        settings.google_oauth_state_ttl_seconds,
        str(current_user.id),
    )

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": _build_scopes(),
        "state": state_token,
    }
    return {"url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}


# Phase 8: bridge endpoint that lets Telegram users (who have no web
# session) start the Google OAuth dance via a one-time token. The token
# is minted on the Telegram side via PermissionService.connect; this
# endpoint trades it for an OAuth state and 302s the browser to Google.
@router.get("/oauth/bridge/{bridge_token}")
def google_oauth_bridge(bridge_token: str):
    from fastapi.responses import RedirectResponse

    _require_google_config()

    redis_client = get_redis()
    bridge_key = f"oauth:bridge:{bridge_token}"
    raw_user_id = redis_client.get(bridge_key)
    if not raw_user_id:
        raise HTTPException(status_code=400, detail="Bridge token invalid or expired")
    redis_client.delete(bridge_key)

    user_id = int(raw_user_id)
    state_token = secrets.token_urlsafe(32)
    redis_client.setex(
        f"oauth:state:{state_token}",
        settings.google_oauth_state_ttl_seconds,
        str(user_id),
    )

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": _build_scopes(),
        "state": state_token,
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/oauth/callback")
def google_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_google_config()

    try:
        redis_client = get_redis()
        state_key = f"oauth:state:{state}"
        user_id = redis_client.get(state_key)
        if not user_id:
            raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
        redis_client.delete(state_key)

        payload = {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.google_oauth_redirect_uri,
        }

        token_resp = httpx.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Token exchange failed")

        tokens = token_resp.json()
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        token_type = tokens.get("token_type", "Bearer")
        scope = tokens.get("scope")
        expires_in = tokens.get("expires_in")

        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo = httpx.get(GOOGLE_USERINFO_URL, headers=headers, timeout=30)
        if userinfo.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to read user profile")

        profile = userinfo.json()
        account_email = profile.get("email") or "unknown"

        account = db.execute(
            select(GoogleAccount).where(GoogleAccount.account_email == account_email)
        ).scalar_one_or_none()

        if not account:
            account = GoogleAccount(account_email=account_email, access_token=access_token)

        account.access_token = access_token
        account.user_id = int(user_id)
        if refresh_token:
            account.refresh_token = refresh_token
        account.token_type = token_type
        account.scope = scope
        if expires_in:
            account.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        db.add(account)
        db.commit()
        db.refresh(account)
    except HTTPException as exc:
        error_msg = quote_plus(str(exc.detail))
        return RedirectResponse(f"/tools?google_error={error_msg}", status_code=303)

    return RedirectResponse("/tools?google_connected=1", status_code=303)


@router.get("/gmail/profile")
def gmail_profile(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = _ensure_token(db, _get_default_account(db, current_user.id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(GMAIL_PROFILE_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Gmail profile")
    return response.json()


@router.get("/calendar/list")
def calendar_list(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    account = _ensure_token(db, _get_default_account(db, current_user.id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(CALENDAR_LIST_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch calendar list")
    return response.json()
