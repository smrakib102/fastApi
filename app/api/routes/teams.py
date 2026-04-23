from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.agent import Agent
from app.models.agent_relation import AgentRelation
from app.models.team import Team
from app.models.team_agent import TeamAgent
from app.models.user import User

router = APIRouter()


@router.get("")
def list_teams(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    teams = db.execute(select(Team).where(Team.user_id == current_user.id)).scalars().all()
    return {
        "items": [
            {"id": team.id, "name": team.name, "description": team.description}
            for team in teams
        ]
    }


@router.post("")
def create_team(
    name: str = Form(...),
    description: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    team = Team(user_id=current_user.id, name=name, description=description)
    db.add(team)
    db.commit()
    db.refresh(team)
    return {"id": team.id, "name": team.name}


@router.post("/{team_id}/agents")
def add_agent_to_team(
    team_id: int,
    agent_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    team = db.execute(
        select(Team).where(Team.id == team_id, Team.user_id == current_user.id)
    ).scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    db.add(TeamAgent(team_id=team_id, agent_id=agent_id))
    db.commit()
    return {"ok": True}


@router.get("/{team_id}/agents")
def list_team_agents(
    team_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    team = db.execute(
        select(Team).where(Team.id == team_id, Team.user_id == current_user.id)
    ).scalar_one_or_none()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    agent_ids = db.execute(
        select(TeamAgent.agent_id).where(TeamAgent.team_id == team_id)
    ).scalars().all()

    agents = db.execute(
        select(Agent).where(Agent.id.in_(agent_ids))
    ).scalars().all()

    return {
        "items": [
            {"id": agent.id, "name": agent.name, "role": agent.role, "category": agent.category}
            for agent in agents
        ]
    }


@router.post("/agents/{agent_id}/subagents")
def add_subagent(
    agent_id: int,
    child_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    parent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    child = db.execute(
        select(Agent).where(Agent.id == child_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()

    if not parent or not child:
        raise HTTPException(status_code=404, detail="Agent not found")

    db.add(AgentRelation(parent_id=parent.id, child_id=child.id))
    db.commit()
    return {"ok": True}


@router.get("/agents/{agent_id}/subagents")
def list_subagents(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    parent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    if not parent:
        raise HTTPException(status_code=404, detail="Agent not found")

    child_ids = db.execute(
        select(AgentRelation.child_id).where(AgentRelation.parent_id == parent.id)
    ).scalars().all()

    agents = db.execute(
        select(Agent).where(Agent.id.in_(child_ids))
    ).scalars().all()

    return {
        "items": [
            {"id": agent.id, "name": agent.name, "role": agent.role, "category": agent.category}
            for agent in agents
        ]
    }
