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
    admin_otp_ttl_seconds: int = 600
    admin_otp_rate_seconds: int = 60
    admin_otp_max_attempts: int = 5
    agent_max_steps: int = 8
    agent_timeout_seconds: int = 120
    agent_memory_steps: int = 6
    agent_summary_every: int = 3
    agent_tool_retry_limit: int = 2
    agent_tool_retry_backoff_seconds: int = 2
    agent_tool_retry_max_backoff_seconds: int = 20
    agent_tool_timeout_seconds: int = 25
    agent_tool_kill_switch_seconds: int = 60
    agent_tool_circuit_breaker_failures: int = 3
    agent_tool_circuit_cooldown_seconds: int = 120
    agent_provider_circuit_breaker_failures: int = 3
    agent_provider_circuit_cooldown_seconds: int = 120
    agent_max_execution_depth: int = 30
    agent_max_plan_cycles: int = 3
    agent_max_tool_failures: int = 4
    agent_max_tool_retries_per_run: int = 6
    agent_max_tokens: int = 8000
    agent_max_cost_usd: float = 1.0
    agent_cost_warning_ratio: float = 0.85
    agent_token_warning_ratio: float = 0.85
    agent_planner_json_repair_attempts: int = 1
    agent_run_stuck_seconds: int = 300
    agent_run_requeue_limit: int = 1
    worker_planner_concurrency: int = 2
    worker_tool_concurrency: int = 4
    worker_long_concurrency: int = 1
    worker_approval_concurrency: int = 1
    worker_heartbeat_seconds: int = 60
    agent_task_max_retries: int = 3
    agent_task_retry_backoff_seconds: int = 15
    auth_secret_key: str = "change-me"
    auth_algorithm: str = "HS256"
    auth_access_token_minutes: int = 60
    auth_cookie_name: str = "aiagent_session"
    auth_token_issuer: str = "ai-agent-system"
    require_jti_for_all_tokens: bool = False
    legacy_user_email: str = "legacy@local"
    secrets_master_key: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: bool = True
    smtp_ssl: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


def _validate_settings(config: Settings) -> None:
    if config.environment in {"staging", "production"}:
        if not config.telegram_webhook_secret:
            raise RuntimeError("telegram_webhook_secret is required in staging/production")
        if not config.tool_api_token:
            raise RuntimeError("tool_api_token is required in staging/production")
        if config.auth_secret_key == "change-me":
            raise RuntimeError("auth_secret_key must be set in staging/production")


_validate_settings(settings)
