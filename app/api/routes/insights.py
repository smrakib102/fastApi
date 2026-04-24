from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_analytics import build_insight_feed

router = APIRouter()


@router.get("/overview")
def get_insights_overview(
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return build_insight_feed(db, current_user.id)


@router.get("/agents/{agent_id}")
def get_agent_insights(
    agent_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return build_insight_feed(db, current_user.id, agent_id)
