from pydantic import Field
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
    # Phase 8: external base URL used to build Telegram → web bridge links
    # (e.g. https://app.example.com). Falls back to the redirect_uri host.
    public_base_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_bot_username: str | None = None
    telegram_link_ttl_seconds: int = 600
    telegram_prompt_ttl_seconds: int = 600
    google_oauth_state_ttl_seconds: int = 600
    # Phase 2: NextAuth shadow OAuth (write-only vault)
    enable_nextauth_oauth: bool = False
    enable_vault_system: bool = False
    nextauth_base_url: str | None = None
    nextauth_post_auth_redirect_url: str | None = None
    nextauth_callback_secret: str | None = None
    nextauth_signature_secret: str | None = None
    nextauth_signature_secondary_secret: str | None = None
    oauth_rollout_percent: int = 0
    oauth_rollout_mode: str = "hash"
    oauth_allowlist_user_ids: str | None = None
    oauth_request_ttl_seconds: int = Field(default=900, validation_alias="OAUTH_REQUEST_TTL_SEC")
    oauth_processed_ttl_seconds: int = 900
    oauth_processing_lock_seconds: int = 60
    oauth_route_ttl_seconds: int = 86400
    oauth_callback_max_skew_seconds: int = 300
    oauth_callback_drift_log_seconds: int = 60
    oauth_metrics_window_seconds: int = 300
    oauth_callback_failure_threshold: int = 25
    oauth_vault_failure_threshold: int = 10
    oauth_duplicate_threshold: int = 50
    oauth_kill_switch_ttl_seconds: int = 3600
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

    # --- Phase 0: feature flags for unified chat refactor ---------------------
    # Master switch for the new ChatService / unified chat pipeline (web + Telegram).
    # When False, all existing flows behave exactly as before.
    unified_chat_enabled: bool = False
    # Sub-flags so phases can be rolled out independently behind the master flag.
    # NOTE: ``unified_chat_web_enabled`` was retired in the consolidation pass
    # (Patch P1). The web /chat/message endpoint follows ``unified_chat_enabled``
    # exclusively now.
    unified_chat_telegram_enabled: bool = False
    unified_chat_nl_agent_builder_enabled: bool = False
    unified_chat_permission_cards_enabled: bool = False

    # --- Phase 7: secrets hardening -----------------------------------------
    # When True, all provider/bot secrets are read **only** from environment
    # variables (or container secrets). Admin/Settings UI inputs are rejected
    # and the corresponding form fields are hidden in templates. This removes
    # the long-standing risk of secrets being entered through the web UI and
    # persisted (encrypted) in the DB.
    secrets_env_only: bool = False

    # --- Phase 5: plugin loader ---------------------------------------------
    # When True, ``app/plugins/*`` is scanned at startup and tool execution
    # consults the plugin registry before falling back to the legacy
    # if/elif chain in app/api/routes/tools.py.
    # Default flipped to True in Patch P7: gmail/calendar handlers now
    # exist as plugins (app/plugins/gmail_tools.py, calendar_tools.py).
    # The legacy if/elif chain remains as a defense-in-depth fallback.
    plugin_loader_enabled: bool = True

    # --- Phase 6: workflow engine -------------------------------------------
    # Gates the WorkflowEngine HTTP endpoint and the run_workflow_task
    # Celery task. The engine itself is import-safe at all times.
    workflow_engine_enabled: bool = False

    # --- Calendar write controls --------------------------------------------
    # When True, calendar.create_request will execute immediately without
    # a manual approval step.
    calendar_auto_approve_writes: bool = False
    # Optional comma-separated user IDs that bypass calendar approvals.
    calendar_auto_approve_user_ids: str | None = None

    # --- v4 production governance: foundation flags --------------------------
    # All flags default to OFF / "off" so this rollout is a pure no-op in prod
    # until each phase is deliberately turned on (DB-backed admin toggle, with
    # .env override authority for the two emergency switches below).
    #
    # Build identity (used by /health):
    build_sha: str | None = None
    build_tag: str | None = None

    # Two emergency switches. .env value ALWAYS wins over DB.
    #   SAFE_MODE_ENABLED   = block side-effecting tools, allow read-only
    #   STRICT_MODE_ENABLED = block ALL tool calls (read + write); chat-only
    safe_mode_enabled: bool = False
    strict_mode_enabled: bool = False

    # Tri-state kernels: "off" | "shadow" (log-only, do not block) | "enforce"
    validation_kernel_mode: str = "off"
    intent_verifier_mode: str = "off"
    safety_kernel_mode: str = "off"
    output_defence_mode: str = "off"
    loop_detector_mode: str = "off"

    # Feature gates (additive; each subsystem stays inert until turned on)
    universal_api_enabled: bool = False
    mcp_enabled: bool = False
    dynamic_tools_enabled: bool = False
    dynamic_tools_allow_fallback: bool = False
    permission_v2_enabled: bool = False
    planner_guardrails_enabled: bool = False
    openclaw_persona_enabled: bool = False
    hitl_enabled: bool = False
    dry_run_enabled: bool = False
    risk_registry_enabled: bool = False
    action_summariser_enabled: bool = False

    # Hardened HTTP client gate. While False, no new code paths are forced
    # through the central client. Flip ON once services/http_client.py lands
    # and its SSRF/DNS-rebinding tests pass.
    allow_http_tools: bool = False

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
        if config.enable_nextauth_oauth or config.enable_vault_system:
            if not config.nextauth_base_url:
                raise RuntimeError("nextauth_base_url is required when NextAuth OAuth is enabled")
            if not config.nextauth_callback_secret:
                raise RuntimeError("nextauth_callback_secret is required when NextAuth OAuth is enabled")
            if not config.nextauth_signature_secret:
                raise RuntimeError("nextauth_signature_secret is required when NextAuth OAuth is enabled")

    # S4: Prevent split-memory. If the master unified-chat flag is on but
    # the Telegram sub-flag was forgotten, force it on so both surfaces
    # land in the same conversations/chat_messages tables. Logged loudly
    # so operators notice the auto-correction.
    if config.unified_chat_enabled and not config.unified_chat_telegram_enabled:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "config_auto_enable",
            extra={
                "flag": "unified_chat_telegram_enabled",
                "reason": "unified_chat_enabled=true forces telegram unified mode "
                "to keep web + Telegram on the same memory pipeline",
            },
        )
        config.unified_chat_telegram_enabled = True


_validate_settings(settings)
