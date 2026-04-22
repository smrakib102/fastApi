from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_legacy_user, require_admin
from app.core.config import settings
from app.models.google_account import GoogleAccount

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


def _require_google_config() -> None:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")


def _build_scopes() -> str:
    scopes = settings.google_oauth_scopes or ""
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    if not scope_list:
        return "openid email profile"
    return " ".join(scope_list)


def _get_default_account(db: Session) -> GoogleAccount:
    legacy_user = get_legacy_user(db)
    account = db.execute(
        select(GoogleAccount).where(
            or_(GoogleAccount.user_id == legacy_user.id, GoogleAccount.user_id.is_(None))
        )
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


@router.get("/oauth/start", dependencies=[Depends(require_admin)])
def google_oauth_start():
    _require_google_config()

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": _build_scopes(),
    }
    return {"url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}"}


@router.get("/oauth/callback")
def google_oauth_callback(
    code: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_google_config()

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

    legacy_user = get_legacy_user(db)
    account.access_token = access_token
    if not account.user_id:
        account.user_id = legacy_user.id
    if refresh_token:
        account.refresh_token = refresh_token
    account.token_type = token_type
    account.scope = scope
    if expires_in:
        account.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    db.add(account)
    db.commit()
    db.refresh(account)

    return {"ok": True, "email": account.account_email}


@router.get("/gmail/profile", dependencies=[Depends(require_admin)])
def gmail_profile(db: Session = Depends(get_db)):
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(GMAIL_PROFILE_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Gmail profile")
    return response.json()


@router.get("/calendar/list", dependencies=[Depends(require_admin)])
def calendar_list(db: Session = Depends(get_db)):
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(CALENDAR_LIST_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch calendar list")
    return response.json()
