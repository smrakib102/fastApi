from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_performance import AgentPerformance
from app.models.user_performance import UserPerformance
from app.models.tool_performance import ToolPerformance
from app.models.model_performance import ModelPerformance


@dataclass
class PlannerGuidance:
    prompt: str | None
    adjusted_steps_left: int
    failure_rate: float


def record_tool_outcome(
    db: Session,
    run_id: int,
    tool_name: str,
    success: bool,
) -> None:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
    if not run:
        return

    perf = db.execute(
        select(ToolPerformance).where(
            ToolPerformance.user_id == run.user_id,
            ToolPerformance.agent_id == run.agent_id,
            ToolPerformance.tool_name == tool_name,
        )
    ).scalar_one_or_none()

    if not perf:
        perf = ToolPerformance(
            user_id=run.user_id,
            agent_id=run.agent_id,
            tool_name=tool_name,
            success_count=0,
            failure_count=0,
            success_rate=0.0,
            score=0.5,
        )
        db.add(perf)

    if success:
        perf.success_count += 1
        perf.last_success_at = datetime.now(timezone.utc)
    else:
        perf.failure_count += 1
        perf.last_failure_at = datetime.now(timezone.utc)

    total = perf.success_count + perf.failure_count
    perf.success_rate = perf.success_count / total if total else 0.0

    alpha = 0.2
    outcome = 1.0 if success else 0.0
    perf.score = (1 - alpha) * float(perf.score or 0.5) + alpha * outcome

    db.commit()


def record_run_outcome(db: Session, run: AgentRun) -> None:
    agent = db.query(Agent).filter(Agent.id == run.agent_id).one_or_none()
    model_name = agent.model if agent and agent.model else "unknown"

    success = run.status == "completed"
    _update_user_performance(db, run, success)
    _update_agent_performance(db, run, success)
    _update_model_performance(db, run, model_name, success)


def get_tool_performance(db: Session, user_id: int, agent_id: int, tools: list[str]) -> dict[str, ToolPerformance]:
    if not tools:
        return {}
    rows = db.execute(
        select(ToolPerformance).where(
            ToolPerformance.user_id == user_id,
            ToolPerformance.agent_id == agent_id,
            ToolPerformance.tool_name.in_(tools),
        )
    ).scalars().all()
    return {row.tool_name: row for row in rows}


def get_planner_guidance(
    db: Session,
    user_id: int,
    agent_id: int,
    steps_left: int,
) -> PlannerGuidance:
    perf = db.execute(
        select(AgentPerformance).where(
            AgentPerformance.user_id == user_id,
            AgentPerformance.agent_id == agent_id,
        )
    ).scalar_one_or_none()

    failure_rate = 0.0
    if perf and perf.run_count:
        failure_rate = perf.failure_count / perf.run_count

    prompt = None
    adjusted_steps = steps_left

    if failure_rate >= 0.4:
        prompt = (
            "Recent runs show higher failure rates. Prefer shorter plans, minimize tool calls, "
            "and add validation steps before risky actions."
        )
        adjusted_steps = max(2, int(steps_left * 0.7))
    elif failure_rate >= 0.2:
        prompt = (
            "Use a safer plan strategy: keep steps concise, avoid unnecessary branching, "
            "and prefer reliable tools."
        )
        adjusted_steps = max(2, int(steps_left * 0.85))

    return PlannerGuidance(prompt=prompt, adjusted_steps_left=adjusted_steps, failure_rate=failure_rate)


def build_insight_feed(db: Session, user_id: int, agent_id: int | None = None) -> dict:
    agent_perf = None
    if agent_id is not None:
        agent_perf = db.execute(
            select(AgentPerformance).where(
                AgentPerformance.user_id == user_id,
                AgentPerformance.agent_id == agent_id,
            )
        ).scalar_one_or_none()

    tool_query = select(ToolPerformance).where(ToolPerformance.user_id == user_id)
    if agent_id is not None:
        tool_query = tool_query.where(ToolPerformance.agent_id == agent_id)
    tools = db.execute(tool_query).scalars().all()

    failing_tools = [
        {
            "tool": tool.tool_name,
            "success_rate": round(float(tool.success_rate or 0.0), 3),
            "attempts": int((tool.success_count or 0) + (tool.failure_count or 0)),
        }
        for tool in tools
        if ((tool.success_count or 0) + (tool.failure_count or 0)) >= 2 and (tool.success_rate or 0.0) < 0.5
    ]

    successful_patterns = [
        {
            "tool": tool.tool_name,
            "success_rate": round(float(tool.success_rate or 0.0), 3),
        }
        for tool in tools
        if ((tool.success_count or 0) + (tool.failure_count or 0)) >= 3 and (tool.success_rate or 0.0) >= 0.8
    ]

    improvements: list[str] = []
    if agent_perf and agent_perf.run_count:
        failure_rate = agent_perf.failure_count / agent_perf.run_count
        if failure_rate >= 0.3:
            improvements.append("Reduce plan length and add validation steps for risky actions.")
        if (agent_perf.cost_efficiency or 0.0) < 0.4:
            improvements.append("Optimize tool usage to reduce cost per run.")

    for tool in failing_tools[:2]:
        improvements.append(f"Investigate tool configuration for {tool['tool']}.")

    models = db.execute(
        select(ModelPerformance).where(ModelPerformance.user_id == user_id)
    ).scalars().all()
    model_summary = [
        {
            "model": model.model_name,
            "success_rate": round(float(model.success_rate or 0.0), 3),
            "avg_cost_usd": round(float(model.avg_cost_usd or 0.0), 4),
        }
        for model in sorted(models, key=lambda item: float(item.success_rate or 0.0), reverse=True)
    ]

    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "recommended_improvements": improvements,
        "failing_tools": failing_tools,
        "successful_patterns": successful_patterns,
        "model_performance": model_summary,
    }

    if agent_perf:
        payload["agent_performance"] = {
            "run_count": agent_perf.run_count,
            "success_rate": round(float(agent_perf.success_rate or 0.0), 3),
            "reliability_score": round(float(agent_perf.reliability_score or 0.0), 3),
            "cost_efficiency": round(float(agent_perf.cost_efficiency or 0.0), 3),
        }

    return payload


def _update_user_performance(db: Session, run: AgentRun, success: bool) -> None:
    perf = db.execute(
        select(UserPerformance).where(UserPerformance.user_id == run.user_id)
    ).scalar_one_or_none()
    if not perf:
        perf = UserPerformance(user_id=run.user_id)
        db.add(perf)

    _apply_run_metrics(perf, run, success)
    perf.last_run_at = run.finished_at
    db.commit()


def _update_agent_performance(db: Session, run: AgentRun, success: bool) -> None:
    perf = db.execute(
        select(AgentPerformance).where(
            AgentPerformance.user_id == run.user_id,
            AgentPerformance.agent_id == run.agent_id,
        )
    ).scalar_one_or_none()
    if not perf:
        perf = AgentPerformance(user_id=run.user_id, agent_id=run.agent_id)
        db.add(perf)

    _apply_run_metrics(perf, run, success)
    perf.last_run_at = run.finished_at
    db.commit()


def _update_model_performance(db: Session, run: AgentRun, model_name: str, success: bool) -> None:
    perf = db.execute(
        select(ModelPerformance).where(
            ModelPerformance.user_id == run.user_id,
            ModelPerformance.model_name == model_name,
        )
    ).scalar_one_or_none()
    if not perf:
        perf = ModelPerformance(user_id=run.user_id, model_name=model_name)
        db.add(perf)

    _apply_model_metrics(perf, run, success)
    db.commit()


def _apply_run_metrics(perf, run: AgentRun, success: bool) -> None:
    perf.run_count = int(perf.run_count or 0) + 1
    if success:
        perf.success_count = int(perf.success_count or 0) + 1
    else:
        perf.failure_count = int(perf.failure_count or 0) + 1

    total = perf.run_count
    perf.success_rate = perf.success_count / total if total else 0.0
    if hasattr(perf, "reliability_score"):
        perf.reliability_score = perf.success_rate

    avg_cost = float(perf.avg_cost_usd or 0.0)
    avg_tokens = float(perf.avg_tokens or 0.0)
    perf.avg_cost_usd = _running_avg(avg_cost, total - 1, float(run.total_cost_usd or 0.0))
    perf.avg_tokens = _running_avg(avg_tokens, total - 1, float(run.total_tokens or 0.0))

    if hasattr(perf, "cost_efficiency"):
        perf.cost_efficiency = _cost_efficiency_score(perf.avg_cost_usd)


def _apply_model_metrics(perf: ModelPerformance, run: AgentRun, success: bool) -> None:
    perf.run_count = int(perf.run_count or 0) + 1
    if success:
        perf.success_count = int(perf.success_count or 0) + 1
    else:
        perf.failure_count = int(perf.failure_count or 0) + 1

    total = perf.run_count
    perf.success_rate = perf.success_count / total if total else 0.0

    avg_cost = float(perf.avg_cost_usd or 0.0)
    avg_tokens = float(perf.avg_tokens or 0.0)
    perf.avg_cost_usd = _running_avg(avg_cost, total - 1, float(run.total_cost_usd or 0.0))
    perf.avg_tokens = _running_avg(avg_tokens, total - 1, float(run.total_tokens or 0.0))


def _running_avg(current_avg: float, count: int, new_value: float) -> float:
    if count <= 0:
        return new_value
    return ((current_avg * count) + new_value) / (count + 1)


def _cost_efficiency_score(avg_cost_usd: float) -> float:
    return 1.0 / (1.0 + max(0.0, avg_cost_usd))
