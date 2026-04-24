import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.core.ai_keys import get_code_provider, get_default_provider
from app.core.model_routing import resolve_provider
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.models.agent_performance import AgentPerformance
from app.models.user import User
from app.services.agent_runtime import AgentRuntimeError, execute_agent_run

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    role: str
    model: str
    tools: list[str] = []
    category: str = "general"


class AgentRunRequest(BaseModel):
    input_text: str | None = None
    async_mode: bool | None = None
from app.services.agent_runtime import AgentRuntimeError, enqueue_agent_run, execute_agent_run


class AgentRunUpdate(BaseModel):
    status: str | None = None
    output_text: str | None = None


class AgentRunStepCreate(BaseModel):
    kind: str | None = None
    status: str | None = None
    content: str | None = None


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
    perf_rows = db.execute(
        select(AgentPerformance).where(AgentPerformance.user_id == current_user.id)
    ).scalars().all()
    perf_map = {row.agent_id: row for row in perf_rows}
    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
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
                "success_rate": float(perf_map.get(agent.id).success_rate)
                if perf_map.get(agent.id)
                else None,
                "reliability_score": float(perf_map.get(agent.id).reliability_score)
                if perf_map.get(agent.id)
                else None,
                "cost_efficiency": float(perf_map.get(agent.id).cost_efficiency)
                if perf_map.get(agent.id)
                else None,
                "run_count": int(perf_map.get(agent.id).run_count)
                if perf_map.get(agent.id)
                else 0,
                "resolved_provider": resolve_provider(
                    db,
                    current_user.id,
                    agent.role,
                    agent.category,
                    default_provider,
                    code_provider,
                )
                if agent.model == "auto"
                else None,
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
    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "model": agent.model,
        "tools": json.loads(agent.tools or "[]"),
        "category": agent.category,
        "status": agent.status,
        "resolved_provider": resolve_provider(
            db,
            current_user.id,
            agent.role,
            agent.category,
            default_provider,
            code_provider,
        )
        if agent.model == "auto"
        else None,
    }


@router.post("/{agent_id}/run")
def run_agent(
    agent_id: int,
    payload: AgentRunRequest,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == current_user.id)
    ).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        if payload.async_mode:
            run = enqueue_agent_run(
                db,
                agent,
                current_user.id,
                payload.input_text,
                source="api",
            )
        else:
            run = execute_agent_run(
                db,
                agent,
                current_user.id,
                payload.input_text,
                source="api",
            )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"run_id": run.id, "status": run.status, "output": run.output_text}


@router.get("/{agent_id}/runs")
def list_agent_runs(
    agent_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    runs = db.execute(
        select(AgentRun)
        .where(AgentRun.agent_id == agent_id, AgentRun.user_id == current_user.id)
        .order_by(AgentRun.created_at.desc())
    ).scalars().all()
    return {
        "items": [
            {
                "id": run.id,
                "status": run.status,
                "input_text": run.input_text,
                "output_text": run.output_text,
                "created_at": run.created_at,
            }
            for run in runs
        ]
    }


@router.get("/{agent_id}/runs/{run_id}")
def get_agent_run(
    agent_id: int,
    run_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.agent_id == agent_id,
            AgentRun.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    step_count = db.execute(
        select(func.count(AgentRunStep.id)).where(AgentRunStep.run_id == run.id)
    ).scalar() or 0

    return {
        "id": run.id,
        "status": run.status,
        "input_text": run.input_text,
        "output_text": run.output_text,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "steps": step_count,
    }


@router.get("/{agent_id}/runs/{run_id}/steps")
def list_agent_run_steps(
    agent_id: int,
    run_id: int,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.agent_id == agent_id,
            AgentRun.user_id == current_user.id,
        )
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
                "content": step.content,
                "tokens_used": step.tokens_used,
                "cost_usd": step.cost_usd,
                "created_at": step.created_at,
                "updated_at": step.updated_at,
            }
        )
    return {"items": items}


def _parse_json_field(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@router.post("/{agent_id}/runs/{run_id}/steps")
def add_agent_run_step(
    agent_id: int,
    run_id: int,
    payload: AgentRunStepCreate,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.agent_id == agent_id,
            AgentRun.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    next_index = (
        db.execute(
            select(func.coalesce(func.max(AgentRunStep.step_index), 0)).where(
                AgentRunStep.run_id == run.id
            )
        ).scalar()
        or 0
    )
    step = AgentRunStep(
        run_id=run.id,
        step_index=next_index + 1,
        kind=payload.kind or "note",
        status=payload.status or "completed",
        content=payload.content,
    )
    db.add(step)
    db.commit()
    db.refresh(step)

    return {
        "id": step.id,
        "step_index": step.step_index,
        "kind": step.kind,
        "status": step.status,
        "content": step.content,
    }


@router.post("/{agent_id}/runs/{run_id}/update")
def update_agent_run(
    agent_id: int,
    run_id: int,
    payload: AgentRunUpdate,
    current_user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    run = db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.agent_id == agent_id,
            AgentRun.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if payload.status:
        run.status = payload.status
    if payload.output_text is not None:
        run.output_text = payload.output_text

    db.add(run)
    db.commit()
    db.refresh(run)

    return {"id": run.id, "status": run.status, "output_text": run.output_text}
