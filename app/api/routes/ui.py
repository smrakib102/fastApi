from pathlib import Path

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.agent import Agent
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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "agents": agents,
            "teams": teams,
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
