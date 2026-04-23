from collections.abc import Generator

from fastapi import Depends, Header, HTTPException, Request
from jose import JWTError
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.user import User


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin_user(current_user: User | None = Depends(get_current_user)) -> User:
    if not current_user or not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def get_legacy_user(db: Session) -> User:
    legacy_email = settings.legacy_user_email
    user = db.execute(select(User).where(User.email == legacy_email)).scalar_one_or_none()
    if user:
        return user

    user = User(email=legacy_email, full_name="Legacy User", hashed_password=None)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _extract_token(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1]

    cookie_name = settings.auth_cookie_name
    if cookie_name:
        return request.cookies.get(cookie_name)

    return None


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User | None:
    token = _extract_token(request, authorization)
    if not token:
        return None

    try:
        payload = decode_access_token(token)
        subject = payload.get("sub")
        if not subject:
            return None
        user_id = int(subject)
    except (JWTError, ValueError):
        return None

    return db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()


def require_user(current_user: User | None = Depends(get_current_user)) -> User:
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return current_user


def get_current_or_legacy_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User:
    current = get_current_user(request=request, db=db, authorization=authorization)
    return current or get_legacy_user(db)
