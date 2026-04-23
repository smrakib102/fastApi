import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.agent import Agent
from app.models.user import User

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    role: str
    model: str
    tools: list[str] = []
    category: str = "general"


@router.get("")
def list_agents(
    category: str | None = None,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    query = select(Agent).where(Agent.user_id == current_user.id)
    if category:
        query = query.where(Agent.category == category)
    agents = db.execute(query).scalars().all()
    return {
        "items": [
            {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role,
                "model": agent.model,
                "tools": json.loads(agent.tools or "[]"),
                "category": agent.category,
                "status": agent.status,
            }
            for agent in agents
        ]
    }


@router.post("")
def create_agent(
    payload: AgentCreate,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    existing = db.execute(select(Agent).where(Agent.name == payload.name)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Agent name already exists")

    agent = Agent(
        user_id=current_user.id,
        name=payload.name,
        role=payload.role,
        model=payload.model,
        tools=json.dumps(payload.tools),
        category=payload.category or "general",
        status="active",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "model": agent.model,
        "tools": json.loads(agent.tools or "[]"),
        "category": agent.category,
        "status": agent.status,
    }
