import json
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_run_step import AgentRunStep


class LongTermMemory(Protocol):
    def get_context(self, user_id: int, agent_id: int) -> str:
        ...


def get_recent_steps(db: Session, run_id: int, limit: int) -> list[AgentRunStep]:
    steps = db.execute(
        select(AgentRunStep)
        .where(AgentRunStep.run_id == run_id)
        .order_by(AgentRunStep.step_index.desc())
        .limit(limit)
    ).scalars().all()
    return list(reversed(steps))


def render_steps_for_prompt(steps: list[AgentRunStep]) -> str:
    lines: list[str] = []
    for step in steps:
        thought = step.thought or ""
        action = step.action_type or step.kind
        tool_name = step.tool_name or ""
        input_text = _safe_json_preview(step.input_json)
        output_text = _safe_json_preview(step.output_json)
        line = (
            f"Step {step.step_number or step.step_index}: action={action}, tool={tool_name}, "
            f"thought={thought}, input={input_text}, output={output_text}"
        )
        lines.append(line)
    return "\n".join(lines)


def build_memory_context(summary: str | None, steps: list[AgentRunStep]) -> str:
    parts: list[str] = []
    if summary:
        parts.append("Summary memory:\n" + summary.strip())
    if steps:
        parts.append("Recent steps:\n" + render_steps_for_prompt(steps))
    return "\n\n".join(parts)


def _safe_json_preview(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    preview = json.dumps(data, ensure_ascii=True)
    if len(preview) > 300:
        return preview[:297] + "..."
    return preview
