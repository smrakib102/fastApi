from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_setting import AdminSetting
from app.models.user_profile import UserProfile


def get_user_key(db: Session, user_id: int, provider: str) -> str | None:
    key_name = f"{provider}_api_key"
    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id, UserProfile.key == key_name)
    ).scalar_one_or_none()
    if profile and profile.value:
        return profile.value

    setting = db.execute(select(AdminSetting).where(AdminSetting.key == key_name)).scalar_one_or_none()
    return setting.value if setting and setting.value else None


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
