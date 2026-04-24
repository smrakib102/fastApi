import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep

logger = logging.getLogger(__name__)


def create_run(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    status: str = "running",
    started_at: datetime | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id,
        user_id=user_id,
        status=status,
        input_text=input_text,
        started_at=started_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def add_step(
    db: Session,
    run_id: int,
    step_number: int,
    action_type: str,
    status: str,
    thought: str | None = None,
    tool_name: str | None = None,
    input_data: dict | None = None,
    output_data: dict | str | None = None,
    kind: str | None = None,
) -> AgentRunStep:
    payload = {
        "step_number": step_number,
        "action_type": action_type,
        "status": status,
        "thought": thought,
        "tool_name": tool_name,
        "input": input_data,
        "output": output_data,
    }

    step = AgentRunStep(
        run_id=run_id,
        step_index=step_number,
        step_number=step_number,
        kind=kind or action_type,
        status=status,
        action_type=action_type,
        thought=thought,
        tool_name=tool_name,
        input_json=json.dumps(input_data, ensure_ascii=True) if input_data is not None else None,
        output_json=json.dumps(output_data, ensure_ascii=True) if output_data is not None else None,
        content=json.dumps(payload, ensure_ascii=True),
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    logger.info(
        "agent_step",
        extra={
            "run_id": run_id,
            "step_number": step_number,
            "action_type": action_type,
            "status": status,
            "tool_name": tool_name,
        },
    )
    return step


def finalize_run(
    db: Session,
    run: AgentRun,
    status: str,
    output_text: str,
    error_message: str | None = None,
) -> None:
    run.status = status
    run.output_text = output_text
    run.error_message = error_message
    run.finished_at = datetime.now(timezone.utc)
    run.updated_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)
