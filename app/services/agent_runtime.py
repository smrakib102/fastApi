import json
import logging
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.llm_client import LLMError
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.services.agent_executor import ToolExecutionError, execute_tool
from app.services.agent_memory import get_recent_steps, render_steps_for_prompt
from app.services.agent_planner import plan_next_action
from app.services.agent_state import add_step, create_run, finalize_run
from app.worker.celery_app import celery_app


class AgentRuntimeError(RuntimeError):
    pass

logger = logging.getLogger(__name__)


def _parse_direct_tool_call(input_text: str | None) -> dict | None:
    if not input_text:
        return None

    stripped = input_text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict) and payload.get("tool"):
                return {
                    "name": payload.get("tool"),
                    "arguments": payload.get("arguments") or {},
                }
        except json.JSONDecodeError:
            return None

    if stripped.lower().startswith("tool:"):
        parts = stripped.split(" ", 2)
        if len(parts) >= 2:
            return {"name": parts[1].strip(), "arguments": {}}

    return None


def execute_agent_run(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    source: str,
) -> AgentRun:
    run = create_run(
        db,
        agent,
        user_id,
        input_text,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    return _execute_run_loop(db, run, agent, user_id, input_text, source)


def enqueue_agent_run(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    source: str,
) -> AgentRun:
    run = create_run(db, agent, user_id, input_text, status="pending", started_at=None)
    celery_app.send_task("app.worker.tasks.run_agent_task", args=[run.id])
    return run


def execute_agent_run_by_id(db: Session, run_id: int) -> AgentRun:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        raise AgentRuntimeError("Run not found")

    agent = db.query(Agent).filter(Agent.id == run.agent_id).one_or_none()
    if not agent:
        raise AgentRuntimeError("Agent not found")

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)

    return _execute_run_loop(db, run, agent, run.user_id, run.input_text, "async")


def _execute_run_loop(
    db: Session,
    run: AgentRun,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    source: str,
) -> AgentRun:
    tools = json.loads(agent.tools or "[]")
    max_steps = settings.agent_max_steps
    timeout_seconds = settings.agent_timeout_seconds
    memory_steps = settings.agent_memory_steps
    start_time = time.monotonic()

    direct_tool = _parse_direct_tool_call(input_text)
    if direct_tool and direct_tool.get("name") in tools:
        tool_args = direct_tool.get("arguments") or {}
        tool_args["user_id"] = user_id
        tool_args["agent_id"] = agent.id
        try:
            result = execute_tool(db, direct_tool["name"], tool_args, retries=1)
            add_step(
                db,
                run.id,
                1,
                "tool",
                "success",
                tool_name=direct_tool["name"],
                input_data=tool_args,
                output_data=result,
            )
            finalize_run(db, run, "completed", json.dumps(result, ensure_ascii=True), None)
            return run
        except ToolExecutionError as exc:
            add_step(
                db,
                run.id,
                1,
                "tool",
                "failed",
                tool_name=direct_tool["name"],
                input_data=tool_args,
                output_data={"error": str(exc)},
            )
            finalize_run(db, run, "failed", str(exc), str(exc))
            return run

    step_number = 1
    while step_number <= max_steps:
        if time.monotonic() - start_time > timeout_seconds:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": "Run timed out"},
            )
            finalize_run(db, run, "failed", "Run timed out", "Run timed out")
            return run

        recent = get_recent_steps(db, run.id, memory_steps) if memory_steps > 0 else []
        memory_text = render_steps_for_prompt(recent) if recent else ""

        try:
            plan = plan_next_action(
                db,
                agent,
                user_id,
                input_text,
                memory_text,
                tools,
                max_steps - step_number + 1,
            )
        except (LLMError, HTTPException, ValueError) as exc:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": str(exc)},
            )
            finalize_run(db, run, "failed", str(exc), str(exc))
            return run

        if plan.action == "final":
            add_step(
                db,
                run.id,
                step_number,
                "final",
                "success",
                thought=plan.thought,
                output_data={"final_answer": plan.final_answer},
            )
            finalize_run(db, run, "completed", plan.final_answer or "", None)
            logger.info("agent_run_completed", extra={"run_id": run.id, "steps": step_number})
            return run

        if plan.action == "tool":
            if not plan.tool_name or plan.tool_name not in tools:
                add_step(
                    db,
                    run.id,
                    step_number,
                    "error",
                    "failed",
                    thought=plan.thought,
                    output_data={"error": "Tool not available"},
                )
                finalize_run(db, run, "failed", "Tool not available", "Tool not available")
                return run

            tool_args = plan.tool_input or {}
            tool_args["user_id"] = user_id
            tool_args["agent_id"] = agent.id

            try:
                result = execute_tool(db, plan.tool_name, tool_args, retries=1)
                add_step(
                    db,
                    run.id,
                    step_number,
                    "tool",
                    "success",
                    thought=plan.thought,
                    tool_name=plan.tool_name,
                    input_data=tool_args,
                    output_data=result,
                )
                logger.info(
                    "agent_tool_success",
                    extra={"run_id": run.id, "tool": plan.tool_name, "step": step_number},
                )
            except ToolExecutionError as exc:
                add_step(
                    db,
                    run.id,
                    step_number,
                    "tool",
                    "failed",
                    thought=plan.thought,
                    tool_name=plan.tool_name,
                    input_data=tool_args,
                    output_data={"error": str(exc)},
                )
                finalize_run(db, run, "failed", str(exc), str(exc))
                return run

        step_number += 1

    add_step(
        db,
        run.id,
        step_number,
        "error",
        "failed",
        output_data={"error": "Max steps reached"},
    )
    finalize_run(db, run, "failed", "Max steps reached", "Max steps reached")
    return run
