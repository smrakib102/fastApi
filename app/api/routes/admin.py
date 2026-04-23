from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin, require_admin_user
from app.core.crypto import encrypt_value, mask_value
from app.core.security import hash_password
from app.models.admin_setting import AdminSetting
from app.models.user import User
from app.models.user_limit import UserLimit
from app.services.audit_log import record_audit

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
    settings = db.execute(select(AdminSetting)).scalars().all()
    settings_map = {setting.key: setting.value for setting in settings}
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
