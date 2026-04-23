from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

PREFIX = "enc:"


def _get_fernet() -> Fernet:
    if not settings.secrets_master_key:
        raise RuntimeError("Missing secrets_master_key")
    return Fernet(settings.secrets_master_key.encode("utf-8"))


def encrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith(PREFIX):
        return value
    token = _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{PREFIX}{token}"


def decrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(PREFIX):
        return value
    token = value[len(PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Invalid encrypted value") from exc


def mask_value(value: str | None) -> str:
    if not value:
        return ""
    return "********"
