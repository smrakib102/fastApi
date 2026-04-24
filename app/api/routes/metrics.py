from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_user
from app.models.agent_run import AgentRun

router = APIRouter()


@router.get("/runs")
def runs_metrics(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    rows = db.execute(
        select(AgentRun.status, func.count(AgentRun.id)).group_by(AgentRun.status)
    ).all()
    return {
        "items": [
            {"status": status, "count": int(count)} for status, count in rows
        ]
    }


@router.get("/errors")
def errors_metrics(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    rows = db.execute(
        select(AgentRun)
        .where(AgentRun.status == "failed")
        .order_by(AgentRun.updated_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "items": [
            {
                "id": run.id,
                "agent_id": run.agent_id,
                "user_id": run.user_id,
                "error_message": run.error_message or run.output_text,
                "updated_at": run.updated_at,
            }
            for run in rows
        ]
    }
