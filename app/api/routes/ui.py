from pathlib import Path

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent
from app.models.team_agent import TeamAgent
from app.models.user_profile import UserProfile
from app.models.team import Team
from app.models.user import User

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/", response_class=HTMLResponse)
def root(request: Request, current_user: User | None = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, current_user: User | None = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=303)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "agents": [],
            "teams": [],
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

    keys = db.execute(
        select(UserProfile).where(
            UserProfile.user_id == current_user.id,
            UserProfile.key.in_(["openai_api_key", "gemini_api_key"]),
        )
    ).scalars().all()
    key_map = {item.key: item.value for item in keys}

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "agents": agents,
            "teams": teams,
            "team_agents": team_map,
            "key_map": key_map,
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
        existing = db.execute(
            select(UserProfile).where(UserProfile.user_id == current_user.id, UserProfile.key == key)
        ).scalar_one_or_none()
        if existing:
            existing.value = value
        else:
            db.add(UserProfile(user_id=current_user.id, key=key, value=value))

    if openai_api_key:
        upsert("openai_api_key", openai_api_key)
    if gemini_api_key:
        upsert("gemini_api_key", gemini_api_key)

    db.commit()
    return RedirectResponse("/dashboard/agents", status_code=303)
