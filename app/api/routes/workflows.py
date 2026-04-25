"""HTTP surface for the WorkflowEngine (Phase 6).

Endpoints
---------
POST /workflows/run     — Synchronous execution. Best for short chains
                          driven from the chat UI.
POST /workflows/dispatch — Enqueue ``run_workflow_task`` on Celery and
                          return immediately. Best for long chains.

All endpoints 404 when ``WORKFLOW_ENGINE_ENABLED`` is off so they do not
exist (from a client perspective) until explicitly enabled.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.core.config import settings
from app.models.user import User
from app.services.workflow_engine import (
    WorkflowError,
    is_enabled,
    workflow_engine,
)


router = APIRouter()


class WorkflowStepIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    type: str = Field(default="tool")
    tool: Optional[str] = None
    args: dict = Field(default_factory=dict)
    retry: int = Field(default=0, ge=0, le=5)
    on_error: str = Field(default="fail")


class WorkflowRunIn(BaseModel):
    steps: list[WorkflowStepIn]
    inputs: dict = Field(default_factory=dict)
    agent_id: Optional[int] = None


def _require_flag() -> None:
    if not is_enabled():
        raise HTTPException(status_code=404, detail="Workflow engine disabled")


@router.post("/run")
def run_workflow(
    payload: WorkflowRunIn,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_flag()
    try:
        result = workflow_engine.run(
            db,
            user_id=current_user.id,
            agent_id=payload.agent_id,
            steps=[step.model_dump() for step in payload.steps],
            inputs=dict(payload.inputs),
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result.to_dict()


@router.post("/dispatch")
def dispatch_workflow(
    payload: WorkflowRunIn,
    current_user: User = Depends(require_user),
):
    _require_flag()
    # Local import keeps Celery out of the import graph when the flag is off.
    from app.worker.celery_app import celery_app

    async_result = celery_app.send_task(
        "app.worker.tasks.run_workflow_task",
        args=[
            current_user.id,
            payload.agent_id,
            [step.model_dump() for step in payload.steps],
            dict(payload.inputs),
        ],
        queue="planner",
    )
    return {"task_id": async_result.id, "status": "queued"}
