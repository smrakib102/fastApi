from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
	agents,
	approvals,
	admin,
	auth,
	calendar,
	gmail,
	google_oauth,
	health,
	insights,
	metrics,
	runs,
	summaries,
	telegram,
	templates,
	teams,
	tool_registry,
	tools,
	usage,
	ui,
)
from app.core.config import settings

app = FastAPI(title=settings.app_name)

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
app.include_router(gmail.router, prefix="/gmail", tags=["gmail"])
app.include_router(approvals.router, prefix="/approvals", tags=["approvals"])
app.include_router(tools.router, prefix="/tools", tags=["tools"])
app.include_router(tool_registry.router, prefix="/tool-registry", tags=["tool-registry"])
app.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
app.include_router(teams.router, prefix="/teams", tags=["teams"])
app.include_router(usage.router, prefix="/usage", tags=["usage"])
app.include_router(summaries.router, prefix="/summaries", tags=["summaries"])
app.include_router(insights.router, prefix="/insights", tags=["insights"])
