from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "ai-agent-system"
    environment: str = "local"
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    database_url: str
    redis_url: str
    admin_token: str
    tool_api_token: str | None = None
    google_gemini_api_key: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    google_oauth_scopes: str | None = None
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_bot_username: str | None = None
    telegram_link_ttl_seconds: int = 600
    telegram_prompt_ttl_seconds: int = 600
    google_oauth_state_ttl_seconds: int = 600
    auth_secret_key: str = "change-me"
    auth_algorithm: str = "HS256"
    auth_access_token_minutes: int = 60
    auth_cookie_name: str = "aiagent_session"
    auth_token_issuer: str = "ai-agent-system"
    legacy_user_email: str = "legacy@local"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
