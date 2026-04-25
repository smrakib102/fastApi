import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.crypto import decrypt_value
from app.models.admin_setting import AdminSetting
from app.models.user_profile import UserProfile


_ENV_NAME_BY_KEY = {
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}


def _env_value(key_name: str) -> str | None:
    env_name = _ENV_NAME_BY_KEY.get(key_name)
    if not env_name:
        return None
    raw = os.environ.get(env_name)
    return raw.strip() if raw else None


def get_user_key(db: Session, user_id: int, provider: str) -> str | None:
    key_name = f"{provider}_api_key"

    # Phase 7: when env-only mode is on, ignore DB entirely.
    if app_settings.secrets_env_only:
        return _env_value(key_name)

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id, UserProfile.key == key_name)
    ).scalar_one_or_none()
    if profile and profile.value:
        return decrypt_value(profile.value)

    setting = db.execute(select(AdminSetting).where(AdminSetting.key == key_name)).scalar_one_or_none()
    if setting and setting.value:
        return decrypt_value(setting.value)
    # Final fallback: env var, even when env-only mode is off, so a fresh
    # deployment without DB rows still works.
    return _env_value(key_name)


def get_default_provider(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "default_model_provider")
    ).scalar_one_or_none()
    return setting.value if setting and setting.value else None


def get_code_provider(db: Session) -> str | None:
    setting = db.execute(
        select(AdminSetting).where(AdminSetting.key == "code_model_provider")
    ).scalar_one_or_none()
    return setting.value if setting and setting.value else None


def get_available_providers(db: Session, user_id: int) -> set[str]:
    available: set[str] = set()
    if get_user_key(db, user_id, "openai"):
        available.add("openai")
    if get_user_key(db, user_id, "gemini"):
        available.add("gemini")
    return available
