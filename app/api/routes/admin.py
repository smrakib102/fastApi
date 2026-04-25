from datetime import datetime, timedelta, timezone
from pathlib import Path

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_admin, require_admin_user
from app.core.config import settings
from app.core.config import settings as app_settings  # explicit alias for use in shadowed scopes
from app.core.redis_client import get_redis
from app.core.crypto import encrypt_value, mask_value
from app.core.security import create_access_token, hash_password
from app.models.admin_setting import AdminSetting
from app.models.user import User
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.models.user_profile import UserProfile
from app.models.user_limit import UserLimit
from app.models.telegram_link import TelegramLink
from app.models.telegram_message import TelegramMessage
from app.models.tool_request import ToolRequest
from app.models.tool_credential import ToolCredential
from app.models.user_limit import UserLimit
from app.services.audit_log import record_audit
from app.services.email_service import send_email

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _upsert_setting(db: Session, key: str, value: str | None) -> None:
    setting = db.execute(select(AdminSetting).where(AdminSetting.key == key)).scalar_one_or_none()
    stored_value = value
    if key in {"telegram_bot_token", "openai_api_key", "gemini_api_key"}:
        stored_value = encrypt_value(value) if value else None
    if setting:
        setting.value = stored_value
    else:
        db.add(AdminSetting(key=key, value=stored_value))
    db.commit()


@router.post("/bootstrap", dependencies=[Depends(require_admin)])
def bootstrap_admin(email: str = Form(...), db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_admin = True
    db.add(user)
    db.commit()
    return {"ok": True, "admin_user_id": user.id}


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request, current_user: User | None = Depends(get_current_user)):
    if current_user and current_user.is_admin:
        return RedirectResponse("/admin/panel", status_code=303)
    return templates.TemplateResponse("admin_login.html", {"request": request})


@router.post("/login")
def admin_login_send(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    normalized_email = email.strip().lower()
    user = db.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if not user or not user.is_admin or not user.is_active or user.is_locked:
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "message": "If the email is eligible, a code has been sent.",
            },
        )

    redis_client = get_redis()
    rate_key = f"admin:otp:sent:{normalized_email}"
    if redis_client.get(rate_key):
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Please wait a minute before requesting another code.",
            },
        )

    code = f"{secrets.randbelow(1000000):06d}"
    redis_client.setex(
        f"admin:otp:{normalized_email}",
        settings.admin_otp_ttl_seconds,
        code,
    )
    redis_client.setex(rate_key, settings.admin_otp_rate_seconds, "1")
    redis_client.delete(f"admin:otp:attempts:{normalized_email}")

    try:
        send_email(
            to_address=normalized_email,
            subject="Your admin login code",
            body=(
                "Your admin login code is: "
                f"{code}\n\nThis code expires in "
                f"{settings.admin_otp_ttl_seconds // 60} minutes."
            ),
        )
    except ValueError:
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Email delivery is not configured. Contact the administrator.",
            },
            status_code=500,
        )

    return templates.TemplateResponse(
        "admin_verify.html",
        {"request": request, "email": normalized_email, "message": "Code sent."},
    )


@router.post("/verify")
def admin_login_verify(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    user = db.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if not user or not user.is_admin or not user.is_active or user.is_locked:
        return templates.TemplateResponse(
            "admin_verify.html",
            {
                "request": request,
                "email": normalized_email,
                "error": "Invalid code.",
            },
            status_code=400,
        )

    redis_client = get_redis()
    expected = redis_client.get(f"admin:otp:{normalized_email}")
    if not expected:
        return templates.TemplateResponse(
            "admin_verify.html",
            {
                "request": request,
                "email": normalized_email,
                "error": "Code expired. Please request a new one.",
            },
            status_code=400,
        )

    if code.strip() != expected:
        attempts_key = f"admin:otp:attempts:{normalized_email}"
        attempts = redis_client.incr(attempts_key)
        if attempts == 1:
            redis_client.expire(attempts_key, settings.admin_otp_ttl_seconds)
        if attempts >= settings.admin_otp_max_attempts:
            redis_client.delete(f"admin:otp:{normalized_email}")
            return templates.TemplateResponse(
                "admin_verify.html",
                {
                    "request": request,
                    "email": normalized_email,
                    "error": "Too many attempts. Please request a new code.",
                },
                status_code=400,
            )
        return templates.TemplateResponse(
            "admin_verify.html",
            {
                "request": request,
                "email": normalized_email,
                "error": "Invalid code.",
            },
            status_code=400,
        )

    redis_client.delete(f"admin:otp:{normalized_email}")
    redis_client.delete(f"admin:otp:attempts:{normalized_email}")

    try:
        token = create_access_token(str(user.id))
    except RuntimeError:
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Auth service unavailable. Please try again."},
            status_code=503,
        )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.auth_access_token_minutes)
    response = RedirectResponse("/admin/panel", status_code=303)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
        max_age=settings.auth_access_token_minutes * 60,
        expires=expires_at,
    )
    record_audit(db, user.id, "admin_otp_login", "user", str(user.id))
    return response


@router.get("/settings")
def list_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    settings = db.execute(select(AdminSetting)).scalars().all()
    return {
        "items": [
            {
                "key": s.key,
                "value": mask_value(s.value) if s.key in {"telegram_bot_token", "openai_api_key", "gemini_api_key"} else s.value,
            }
            for s in settings
        ]
    }


@router.get("/plugins")
def list_plugins(
    current_user: User = Depends(require_admin_user),
):
    """Phase 5: list registered plugins. Returns an empty list when the
    plugin loader is disabled, so callers can use this as a feature probe.
    """
    if not app_settings.plugin_loader_enabled:
        return {"enabled": False, "items": []}
    from app.plugins import plugin_registry

    plugin_registry.discover()
    return {
        "enabled": True,
        "items": [
            {
                "name": p.name,
                "category": p.category,
                "description": p.description,
                "required_scopes": list(p.required_scopes),
            }
            for p in plugin_registry.all()
        ],
    }


@router.post("/settings")
def update_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
    telegram_bot_token: str | None = Form(default=None),
    telegram_bot_username: str | None = Form(default=None),
    openai_api_key: str | None = Form(default=None),
    gemini_api_key: str | None = Form(default=None),
    default_model_provider: str | None = Form(default=None),
    code_model_provider: str | None = Form(default=None),
):
    # Phase 7: secrets-via-UI lockdown.
    if settings.secrets_env_only and (telegram_bot_token or openai_api_key or gemini_api_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "SECRETS_ENV_ONLY is enabled. Provider keys and the Telegram bot "
                "token must be supplied via environment variables, not the UI."
            ),
        )
    if telegram_bot_token:
        _upsert_setting(db, "telegram_bot_token", telegram_bot_token)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "telegram_bot_token")
    if telegram_bot_username is not None:
        _upsert_setting(db, "telegram_bot_username", telegram_bot_username)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "telegram_bot_username")
    if openai_api_key:
        _upsert_setting(db, "openai_api_key", openai_api_key)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "openai_api_key")
    if gemini_api_key:
        _upsert_setting(db, "gemini_api_key", gemini_api_key)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "gemini_api_key")
    if default_model_provider is not None:
        _upsert_setting(db, "default_model_provider", default_model_provider)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "default_model_provider")
    if code_model_provider is not None:
        _upsert_setting(db, "code_model_provider", code_model_provider)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "code_model_provider")

    return {"ok": True}


@router.post("/limits")
def set_user_limits(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
    user_id: int = Form(...),
    provider: str = Form(...),
    daily_limit: int | None = Form(default=None),
    monthly_limit: int | None = Form(default=None),
):
    limit = db.execute(
        select(UserLimit).where(
            UserLimit.user_id == user_id, UserLimit.provider == provider
        )
    ).scalar_one_or_none()

    if limit:
        limit.daily_limit = daily_limit
        limit.monthly_limit = monthly_limit
    else:
        limit = UserLimit(
            user_id=user_id,
            provider=provider,
            daily_limit=daily_limit,
            monthly_limit=monthly_limit,
        )
        db.add(limit)

    db.commit()
    return {"ok": True}


@router.get("/panel", response_class=HTMLResponse)
def admin_panel(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    settings_rows = db.execute(select(AdminSetting)).scalars().all()
    settings_map = {row.key: row.value for row in settings_rows}
    masked_settings = {
        key: mask_value(value)
        for key, value in settings_map.items()
        if key in {"telegram_bot_token", "openai_api_key", "gemini_api_key"}
    }
    limits = db.execute(select(UserLimit)).scalars().all()
    users = db.execute(select(User)).scalars().all()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": current_user,
            "settings": settings_map,
            "masked_settings": masked_settings,
            "limits": limits,
            "users": users,
            "secrets_env_only": app_settings.secrets_env_only,
        },
    )


@router.post("/panel/settings")
def admin_panel_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
    telegram_bot_token: str | None = Form(default=None),
    telegram_bot_username: str | None = Form(default=None),
    openai_api_key: str | None = Form(default=None),
    gemini_api_key: str | None = Form(default=None),
    default_model_provider: str | None = Form(default=None),
    code_model_provider: str | None = Form(default=None),
):
    # Phase 7: secrets-via-UI lockdown.
    if settings.secrets_env_only and (telegram_bot_token or openai_api_key or gemini_api_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "SECRETS_ENV_ONLY is enabled. Set the Telegram bot token and "
                "provider keys via environment variables instead."
            ),
        )
    if telegram_bot_token:
        _upsert_setting(db, "telegram_bot_token", telegram_bot_token)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "telegram_bot_token")
    if telegram_bot_username is not None:
        _upsert_setting(db, "telegram_bot_username", telegram_bot_username)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "telegram_bot_username")
    if openai_api_key:
        _upsert_setting(db, "openai_api_key", openai_api_key)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "openai_api_key")
    if gemini_api_key:
        _upsert_setting(db, "gemini_api_key", gemini_api_key)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "gemini_api_key")
    if default_model_provider is not None:
        _upsert_setting(db, "default_model_provider", default_model_provider)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "default_model_provider")
    if code_model_provider is not None:
        _upsert_setting(db, "code_model_provider", code_model_provider)
        record_audit(db, current_user.id, "update_setting", "admin_setting", "code_model_provider")

    return RedirectResponse("/admin/panel", status_code=303)


@router.post("/panel/limits")
def admin_panel_limits(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
    user_id: int = Form(...),
    provider: str = Form(...),
    daily_limit: int | None = Form(default=None),
    monthly_limit: int | None = Form(default=None),
):
    limit = db.execute(
        select(UserLimit).where(
            UserLimit.user_id == user_id, UserLimit.provider == provider
        )
    ).scalar_one_or_none()

    if limit:
        limit.daily_limit = daily_limit
        limit.monthly_limit = monthly_limit
    else:
        db.add(
            UserLimit(
                user_id=user_id,
                provider=provider,
                daily_limit=daily_limit,
                monthly_limit=monthly_limit,
            )
        )
    db.commit()

    return RedirectResponse("/admin/panel", status_code=303)


@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    users = db.execute(select(User)).scalars().all()
    return {
        "items": [
            {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active,
                "is_locked": user.is_locked,
            }
            for user in users
        ]
    }


@router.post("/users/lock")
def lock_user(
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    target = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.is_locked = True
    db.add(target)
    db.commit()
    record_audit(db, current_user.id, "lock_user", "user", str(user_id))
    return {"ok": True}


@router.post("/users/unlock")
def unlock_user(
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    target = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.is_locked = False
    db.add(target)
    db.commit()
    record_audit(db, current_user.id, "unlock_user", "user", str(user_id))
    return {"ok": True}


@router.post("/users/delete")
def delete_user(
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    target = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if target.email == settings.legacy_user_email:
        raise HTTPException(status_code=400, detail="Cannot delete legacy user")

    db.query(AgentRunStep).filter(AgentRunStep.run_id.in_(
        select(AgentRun.id).where(AgentRun.user_id == target.id)
    )).delete(synchronize_session=False)
    db.query(AgentRun).filter(AgentRun.user_id == target.id).delete(synchronize_session=False)
    db.query(Agent).filter(Agent.user_id == target.id).delete(synchronize_session=False)
    db.query(UserProfile).filter(UserProfile.user_id == target.id).delete(synchronize_session=False)
    db.query(UserLimit).filter(UserLimit.user_id == target.id).delete(synchronize_session=False)
    db.query(TelegramLink).filter(TelegramLink.user_id == target.id).delete(synchronize_session=False)
    db.query(TelegramMessage).filter(TelegramMessage.user_id == target.id).delete(synchronize_session=False)
    db.query(ToolRequest).filter(ToolRequest.user_id == target.id).delete(synchronize_session=False)
    db.query(ToolCredential).filter(ToolCredential.user_id == target.id).delete(synchronize_session=False)

    db.delete(target)
    db.commit()
    record_audit(db, current_user.id, "delete_user", "user", str(user_id))
    return {"ok": True}


@router.post("/users/reset-password")
def reset_password(
    user_id: int = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
    target = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.hashed_password = hash_password(new_password)
    db.add(target)
    db.commit()
    record_audit(db, current_user.id, "reset_password", "user", str(user_id))
    return {"ok": True}
