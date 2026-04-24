from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, revoke_session, revoke_user_sessions, verify_password
from app.models.user import User

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if not user or not user.hashed_password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=400,
        )
    if not user.is_active or user.is_locked:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Account is locked."},
            status_code=403,
        )

    if not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=400,
        )

    try:
        token = create_access_token(str(user.id))
    except RuntimeError:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Auth service unavailable. Please try again."},
            status_code=503,
        )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.auth_access_token_minutes)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
        max_age=settings.auth_access_token_minutes * 60,
        expires=expires_at,
    )
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
def register_submit(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email is already registered."},
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Password must be at least 8 characters."},
            status_code=400,
        )

    user = User(
        email=email,
        full_name=full_name or None,
        hashed_password=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        token = create_access_token(str(user.id))
    except RuntimeError:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Auth service unavailable. Please try again."},
            status_code=503,
        )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.auth_access_token_minutes)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
        max_age=settings.auth_access_token_minutes * 60,
        expires=expires_at,
    )
    return response


@router.get("/logout")
def logout(request: Request, authorization: str | None = Header(default=None)):
    token = None
    auth_header = authorization or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
    if not token:
        token = request.cookies.get(settings.auth_cookie_name)
    if token:
        try:
            payload = decode_access_token(token)
            user_id = payload.get("sub")
            jti = payload.get("jti")
            if user_id:
                revoke_user_sessions(str(user_id))
            elif jti:
                revoke_session(jti)
        except JWTError:
            pass
        except RuntimeError:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Auth service unavailable. Please try again."},
                status_code=503,
            )

    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(settings.auth_cookie_name)
    return response


@router.get("/me")
def auth_me(current_user: User | None = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_active": current_user.is_active,
    }
