from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin, require_admin_user
from app.models.admin_setting import AdminSetting
from app.models.user import User
from app.models.user_limit import UserLimit

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _upsert_setting(db: Session, key: str, value: str | None) -> None:
    setting = db.execute(select(AdminSetting).where(AdminSetting.key == key)).scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(AdminSetting(key=key, value=value))
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
    return {"items": [{"key": s.key, "value": s.value} for s in settings]}


@router.post("/settings")
def update_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_user),
    telegram_bot_token: str | None = Form(default=None),
    telegram_bot_username: str | None = Form(default=None),
    openai_api_key: str | None = Form(default=None),
    gemini_api_key: str | None = Form(default=None),
):
    if telegram_bot_token is not None:
        _upsert_setting(db, "telegram_bot_token", telegram_bot_token)
    if telegram_bot_username is not None:
        _upsert_setting(db, "telegram_bot_username", telegram_bot_username)
    if openai_api_key is not None:
        _upsert_setting(db, "openai_api_key", openai_api_key)
    if gemini_api_key is not None:
        _upsert_setting(db, "gemini_api_key", gemini_api_key)

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
    limits = db.execute(select(UserLimit)).scalars().all()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": current_user,
            "settings": settings_map,
            "limits": limits,
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
):
    if telegram_bot_token is not None:
        _upsert_setting(db, "telegram_bot_token", telegram_bot_token)
    if telegram_bot_username is not None:
        _upsert_setting(db, "telegram_bot_username", telegram_bot_username)
    if openai_api_key is not None:
        _upsert_setting(db, "openai_api_key", openai_api_key)
    if gemini_api_key is not None:
        _upsert_setting(db, "gemini_api_key", gemini_api_key)

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
