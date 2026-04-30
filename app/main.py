from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
	agents,
	approvals,
	admin,
	auth,
	calendar,
	chat,
	gmail,
	google_oauth,
	health,
	insights,
	metrics,
	oauth_vault,
	runs,
	summaries,
	telegram,
	templates,
	teams,
	tool_registry,
	tools,
	usage,
	ui,
	workflows,
)
from app.core.config import settings
from app.services.telegram_health import log_telegram_status_on_startup

_is_prod = settings.environment in {"staging", "production"}
app = FastAPI(
    title=settings.app_name,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(health.router, tags=["health"])
app.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
app.include_router(ui.router, tags=["ui"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(agents.router, prefix="/agents", tags=["agents"])
app.include_router(runs.router, prefix="/runs", tags=["runs"])
app.include_router(telegram.router, prefix="/telegram", tags=["telegram"])
app.include_router(templates.router, prefix="/templates", tags=["templates"])
app.include_router(google_oauth.router, prefix="/google", tags=["google"])
app.include_router(oauth_vault.router, prefix="/oauth", tags=["oauth"])
app.include_router(gmail.router, prefix="/gmail", tags=["gmail"])
app.include_router(approvals.router, prefix="/approvals", tags=["approvals"])
app.include_router(tools.router, prefix="/tools", tags=["tools"])
app.include_router(tool_registry.router, prefix="/tool-registry", tags=["tool-registry"])
app.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
app.include_router(teams.router, prefix="/teams", tags=["teams"])
app.include_router(usage.router, prefix="/usage", tags=["usage"])
app.include_router(summaries.router, prefix="/summaries", tags=["summaries"])
app.include_router(insights.router, prefix="/insights", tags=["insights"])
# Phase 2c: unified chat HTTP API (gated by UNIFIED_CHAT_ENABLED).
app.include_router(chat.router, prefix="/chat", tags=["chat"])
# Phase 6: workflow engine HTTP API (gated by WORKFLOW_ENGINE_ENABLED).
app.include_router(workflows.router, prefix="/workflows", tags=["workflows"])


# Phase 5: discover plugins at startup when enabled. Failures are logged
# inside discover() and never abort boot.
@app.on_event("startup")
def _discover_plugins() -> None:
    if settings.plugin_loader_enabled:
        from app.plugins import plugin_registry

        plugin_registry.discover()


@app.on_event("startup")
def _log_telegram_status() -> None:
    log_telegram_status_on_startup()
