import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.models.user import User

router = APIRouter()


@router.get("")
def list_runs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id, isouter=True)
        .where(AgentRun.user_id == current_user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .offset(offset)
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

    items = []
    for run, agent_name in rows:
        items.append(
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
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "steps": step_counts.get(run.id, 0),
            }
        )

    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{run_id}")
def get_run(
    run_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(AgentRun, Agent.name)
        .join(Agent, Agent.id == AgentRun.agent_id, isouter=True)
        .where(AgentRun.id == run_id, AgentRun.user_id == current_user.id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    run, agent_name = row
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "agent_name": agent_name,
        "status": run.status,
        "input_text": run.input_text,
        "output_text": run.output_text,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.get("/{run_id}/timeline")
def get_run_timeline(
    run_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == current_user.id)
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    steps = db.execute(
        select(AgentRunStep)
        .where(AgentRunStep.run_id == run.id)
        .order_by(AgentRunStep.step_index.asc())
    ).scalars().all()

    items = []
    for step in steps:
        content = {}
        if step.content:
            try:
                content = json.loads(step.content)
            except json.JSONDecodeError:
                content = {}
        input_value = _parse_json_field(step.input_json) or content.get("input")
        output_value = _parse_json_field(step.output_json) or content.get("output")
        items.append(
            {
                "id": step.id,
                "step_index": step.step_index,
                "step_number": step.step_number or step.step_index,
                "kind": step.kind,
                "action_type": step.action_type or content.get("action_type"),
                "thought": step.thought or content.get("thought"),
                "tool_name": step.tool_name or content.get("tool_name"),
                "input": input_value,
                "output": output_value,
                "reasoning": _parse_json_field(step.reasoning_json) or content.get("reasoning"),
                "status": step.status,
                "created_at": step.created_at,
                "updated_at": step.updated_at,
            }
        )

    return {
        "run": {
            "id": run.id,
            "agent_id": run.agent_id,
            "status": run.status,
            "input_text": run.input_text,
            "output_text": run.output_text,
            "error_message": run.error_message,
            "summary_memory": run.summary_memory,
            "total_tokens": run.total_tokens,
            "total_cost_usd": run.total_cost_usd,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        },
        "steps": items,
    }


@router.get("/{run_id}/stream")
def stream_run_updates(
    run_id: int,
    since_step: int | None = Query(default=None, ge=0),
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == current_user.id)
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    query = (
        select(AgentRunStep)
        .where(AgentRunStep.run_id == run.id)
        .order_by(AgentRunStep.step_index.asc())
    )
    if since_step is not None:
        query = query.where(AgentRunStep.step_index > since_step)

    steps = db.execute(query).scalars().all()
    items = []
    latest_step = since_step or 0
    for step in steps:
        content = {}
        if step.content:
            try:
                content = json.loads(step.content)
            except json.JSONDecodeError:
                content = {}
        input_value = _parse_json_field(step.input_json) or content.get("input")
        output_value = _parse_json_field(step.output_json) or content.get("output")
        items.append(
            {
                "id": step.id,
                "step_index": step.step_index,
                "step_number": step.step_number or step.step_index,
                "kind": step.kind,
                "action_type": step.action_type or content.get("action_type"),
                "thought": step.thought or content.get("thought"),
                "tool_name": step.tool_name or content.get("tool_name"),
                "input": input_value,
                "output": output_value,
                "reasoning": _parse_json_field(step.reasoning_json) or content.get("reasoning"),
                "status": step.status,
                "tokens_used": step.tokens_used,
                "cost_usd": step.cost_usd,
                "created_at": step.created_at,
                "updated_at": step.updated_at,
            }
        )
        latest_step = max(latest_step, step.step_index)

    return {
        "run": {
            "id": run.id,
            "status": run.status,
            "output_text": run.output_text,
            "error_message": run.error_message,
            "summary_memory": run.summary_memory,
            "total_tokens": run.total_tokens,
            "total_cost_usd": run.total_cost_usd,
            "updated_at": run.updated_at,
            "finished_at": run.finished_at,
        },
        "steps": items,
        "latest_step": latest_step,
    }


def _parse_json_field(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
