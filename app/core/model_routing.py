from app.core.ai_keys import get_available_providers


CODE_KEYWORDS = {"code", "coding", "developer", "dev", "program", "engineer"}


def _is_code_agent(role: str | None, category: str | None) -> bool:
    role_text = (role or "").lower()
    category_text = (category or "").lower()
    return any(keyword in role_text for keyword in CODE_KEYWORDS) or any(
        keyword in category_text for keyword in CODE_KEYWORDS
    )


def resolve_provider(
    db,
    user_id: int,
    role: str | None,
    category: str | None,
    default_provider: str | None,
    code_provider: str | None,
) -> str | None:
    available = get_available_providers(db, user_id)
    if not available:
        return None

    wants_code = _is_code_agent(role, category)
    if wants_code and code_provider and code_provider in available:
        return code_provider

    if default_provider and default_provider in available:
        return default_provider

    if len(available) == 1:
        return next(iter(available))

    if "openai" in available:
        return "openai"
    if "gemini" in available:
        return "gemini"

    return next(iter(available))
