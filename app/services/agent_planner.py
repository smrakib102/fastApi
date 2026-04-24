import json
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.ai_keys import get_code_provider, get_default_provider, get_user_key
from app.core.llm_client import LLMError, call_gemini, call_openai_chat
from app.core.model_routing import resolve_provider
from app.models.agent import Agent
from app.services.usage_limits import check_and_record_usage

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"

logger = logging.getLogger(__name__)


@dataclass
class PlannerOutput:
    thought: str
    action: str
    tool_name: str | None
    tool_input: dict
    final_answer: str | None
    raw: str


def plan_next_action(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    memory_text: str,
    tools: list[str],
    steps_left: int,
) -> PlannerOutput:
    provider, model = _get_provider_and_model(db, agent, user_id)
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        raise LLMError(f"Missing API key for {provider}")

    tool_list = ", ".join(tools) if tools else "(none)"
    system_prompt = (
        "You are an agent planner. Return ONLY valid JSON with the exact schema:\n"
        '{"thought":"...","action":"tool|final","tool_name":"...","input":{},"final_answer":"..."}\n'
        "Rules:\n"
        "- action must be tool or final.\n"
        "- If action=tool, tool_name and input are required.\n"
        "- If action=final, final_answer is required.\n"
        "- No extra keys. No markdown. No code fences.\n"
        f"Available tools: {tool_list}\n"
        f"Steps left: {steps_left}\n"
    )

    user_prompt = "User request: " + (input_text or "(no input)")
    if memory_text:
        user_prompt += "\n\nRecent steps:\n" + memory_text

    if provider == "openai":
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        raw, tokens = call_openai_chat(api_key, model, messages)
    else:
        prompt = system_prompt + "\n" + user_prompt
        raw, tokens = call_gemini(api_key, model, prompt)

    check_and_record_usage(db, user_id, provider, tokens)

    parsed = _parse_planner_json(raw)
    if not parsed:
        logger.warning("planner_json_parse_failed")
        return PlannerOutput(
            thought="",
            action="final",
            tool_name=None,
            tool_input={},
            final_answer="Planner output invalid. Please try again.",
            raw=raw,
        )

    action = str(parsed.get("action", "")).strip()
    thought = str(parsed.get("thought", "")).strip()
    tool_name = parsed.get("tool_name")
    tool_input = parsed.get("input") if isinstance(parsed.get("input"), dict) else {}
    final_answer = parsed.get("final_answer")

    if action not in {"tool", "final"}:
        return PlannerOutput(
            thought=thought,
            action="final",
            tool_name=None,
            tool_input={},
            final_answer="Planner action invalid. Please try again.",
            raw=raw,
        )

    if action == "tool" and (not tool_name or not isinstance(tool_input, dict)):
        return PlannerOutput(
            thought=thought,
            action="final",
            tool_name=None,
            tool_input={},
            final_answer="Planner tool data invalid. Please try again.",
            raw=raw,
        )

    if action == "final" and not final_answer:
        return PlannerOutput(
            thought=thought,
            action="final",
            tool_name=None,
            tool_input={},
            final_answer="Planner final answer missing. Please try again.",
            raw=raw,
        )

    return PlannerOutput(
        thought=thought,
        action=action,
        tool_name=str(tool_name) if tool_name else None,
        tool_input=tool_input,
        final_answer=str(final_answer) if final_answer else None,
        raw=raw,
    )


def _parse_planner_json(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _get_provider_and_model(db: Session, agent: Agent, user_id: int) -> tuple[str, str]:
    if agent.model != "auto":
        provider = "openai" if agent.model.startswith("gpt-") else "gemini"
        return provider, agent.model

    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
    provider = resolve_provider(db, user_id, agent.role, agent.category, default_provider, code_provider)
    if not provider:
        raise LLMError("No model provider available")
    model = OPENAI_DEFAULT_MODEL if provider == "openai" else GEMINI_DEFAULT_MODEL
    return provider, model
