from pathlib import Path

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.ai_keys import get_code_provider, get_default_provider
from app.core.crypto import encrypt_value
from app.core.model_routing import resolve_provider
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.models.agent_performance import AgentPerformance
from app.models.team_agent import TeamAgent
from app.models.user_profile import UserProfile
from app.models.team import Team
from app.models.user import User
from app.services.agent_runtime import AgentRuntimeError, execute_agent_run
from app.services.audit_log import record_audit

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/", response_class=HTMLResponse)
def root(request: Request, current_user: User | None = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    agent_count = db.execute(
        select(func.count(Agent.id)).where(Agent.user_id == current_user.id)
    ).scalar()
    if not agent_count:
        return RedirectResponse("/onboarding", status_code=303)

    latest_runs = db.execute(
        select(AgentRun)
        .where(AgentRun.user_id == current_user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(6)
    ).scalars().all()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "latest_runs": latest_runs,
        },
    )


@router.get("/insights", response_class=HTMLResponse)
def insights_dashboard(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    agents = db.execute(
        select(Agent).where(Agent.user_id == current_user.id).order_by(Agent.created_at.desc())
    ).scalars().all()

    return templates.TemplateResponse(
        "insights.html",
        {
            "request": request,
            "user": current_user,
            "agents": agents,
        },
    )


@router.get("/dashboard/agents", response_class=HTMLResponse)
def dashboard_agents(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    agents = db.execute(
        select(Agent).where(Agent.user_id == current_user.id).order_by(Agent.created_at.desc())
    ).scalars().all()

    perf_rows = db.execute(
        select(AgentPerformance).where(AgentPerformance.user_id == current_user.id)
    ).scalars().all()
    perf_map = {row.agent_id: row for row in perf_rows}

    teams = db.execute(
        select(Team).where(Team.user_id == current_user.id).order_by(Team.created_at.desc())
    ).scalars().all()

    team_map: dict[int, list[Agent]] = {}
    if teams:
        team_ids = [team.id for team in teams]
        mappings = db.execute(
            select(TeamAgent).where(TeamAgent.team_id.in_(team_ids))
        ).scalars().all()

        agent_by_id = {agent.id: agent for agent in agents}
        for mapping in mappings:
            team_map.setdefault(mapping.team_id, [])
            agent = agent_by_id.get(mapping.agent_id)
            if agent:
                team_map[mapping.team_id].append(agent)

    latest_runs = db.execute(
        select(AgentRun)
        .where(AgentRun.user_id == current_user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(12)
    ).scalars().all()

    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
    for agent in agents:
        if agent.model == "auto":
            agent.resolved_provider = resolve_provider(
                db,
                current_user.id,
                agent.role,
                agent.category,
                default_provider,
                code_provider,
            )
        else:
            agent.resolved_provider = None

    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "user": current_user,
            "agents": agents,
            "agent_perf": perf_map,
            "teams": teams,
            "team_agents": team_map,
            "latest_runs": latest_runs,
        },
    )


@router.post("/dashboard/agents")
def dashboard_create_agent(
    request: Request,
    name: str = Form(...),
    role: str = Form(...),
    model: str = Form(...),
    category: str = Form("general"),
    tools: str = Form(""),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    existing = db.execute(select(Agent).where(Agent.name == name)).scalar_one_or_none()
    if existing:
        return RedirectResponse("/dashboard/agents?error=name", status_code=303)

    tools_list = [t.strip() for t in tools.split(",") if t.strip()]
    agent = Agent(
        user_id=current_user.id,
        name=name,
        role=role,
        model=model,
        tools=json.dumps(tools_list),
        category=category or "general",
        status="active",
    )
    db.add(agent)
    db.commit()

    return RedirectResponse("/dashboard/agents", status_code=303)


@router.post("/dashboard/teams")
def dashboard_create_team(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    team = Team(user_id=current_user.id, name=name, description=description or None)
    db.add(team)
    db.commit()

    return RedirectResponse("/dashboard/agents", status_code=303)


@router.post("/dashboard/teams/assign")
def dashboard_assign_agent(
    request: Request,
    team_id: int = Form(...),
    agent_id: int = Form(...),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    db.add(TeamAgent(team_id=team_id, agent_id=agent_id))
    db.commit()
    return RedirectResponse("/dashboard/agents", status_code=303)


@router.post("/dashboard/teams/remove")
def dashboard_remove_agent(
    request: Request,
    team_id: int = Form(...),
    agent_id: int = Form(...),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    mapping = db.execute(
        select(TeamAgent).where(TeamAgent.team_id == team_id, TeamAgent.agent_id == agent_id)
    ).scalar_one_or_none()
    if mapping:
        db.delete(mapping)
        db.commit()
    return RedirectResponse("/dashboard/agents", status_code=303)


@router.post("/dashboard/keys")
def dashboard_update_keys(
    request: Request,
    openai_api_key: str = Form(""),
    gemini_api_key: str = Form(""),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    def upsert(key: str, value: str):
        stored = encrypt_value(value)
        existing = db.execute(
            select(UserProfile).where(UserProfile.user_id == current_user.id, UserProfile.key == key)
        ).scalar_one_or_none()
        if existing:
            existing.value = stored
        else:
            db.add(UserProfile(user_id=current_user.id, key=key, value=stored))

    if openai_api_key:
        upsert("openai_api_key", openai_api_key)
        record_audit(db, current_user.id, "update_key", "user_profile", "openai_api_key")
    if gemini_api_key:
        upsert("gemini_api_key", gemini_api_key)
        record_audit(db, current_user.id, "update_key", "user_profile", "gemini_api_key")

    db.commit()
    return RedirectResponse("/dashboard/agents", status_code=303)


@router.post("/dashboard/agents/run")
def dashboard_run_agent(
    request: Request,
    agent_id: int = Form(...),
    input_text: str = Form(""),
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    if not agent:
        return RedirectResponse("/dashboard/agents?error=missing", status_code=303)

    try:
        execute_agent_run(db, agent, current_user.id, input_text or None, source="ui")
    except AgentRuntimeError:
        return RedirectResponse("/dashboard/agents?error=runtime", status_code=303)

    return RedirectResponse("/dashboard/agents", status_code=303)


@router.get("/chat", response_class=HTMLResponse)
def chat_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/agents/new", response_class=HTMLResponse)
def agent_builder(
    request: Request,
    current_user: User | None = Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        "agents_new.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/tools", response_class=HTMLResponse)
def tools_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        "tools.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    keys = db.execute(
        select(UserProfile).where(
            UserProfile.user_id == current_user.id,
            UserProfile.key.in_(["openai_api_key", "gemini_api_key"]),
        )
    ).scalars().all()
    key_map = {item.key: item.value for item in keys}

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": current_user,
            "key_map": key_map,
        },
    )


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    agent_count = db.execute(
        select(func.count(Agent.id)).where(Agent.user_id == current_user.id)
    ).scalar()
    if agent_count:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/dashboard/runs", response_class=HTMLResponse)
def dashboard_runs(
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    rows = db.execute(
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id, isouter=True)
        .where(AgentRun.user_id == current_user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(200)
    ).all()

    run_ids = [row[0].id for row in rows]
    step_counts: dict[int, int] = {}
    if run_ids:
        counts = db.execute(
            select(AgentRunStep.run_id, func.count(AgentRunStep.id))
            .where(AgentRunStep.run_id.in_(run_ids))
            .group_by(AgentRunStep.run_id)
        ).all()
        step_counts = {run_id: count for run_id, count in counts}

    runs = []
    for run, agent_name in rows:
        runs.append(
            {
                "id": run.id,
                "agent_id": run.agent_id,
                "agent_name": agent_name,
                "status": run.status,
                "input_text": run.input_text,
                "output_text": run.output_text,
                "error_message": run.error_message,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "steps": step_counts.get(run.id, 0),
            }
        )

    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "user": current_user,
            "runs": runs,
        },
    )


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse)
def dashboard_run_detail(
    run_id: int,
    request: Request,
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    row = db.execute(
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id, isouter=True)
        .where(AgentRun.id == run_id, AgentRun.user_id == current_user.id)
    ).first()

    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    run, agent_name = row
    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "user": current_user,
            "run": run,
            "agent_name": agent_name,
        },
    )
