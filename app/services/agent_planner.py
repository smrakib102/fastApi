import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.ai_keys import get_code_provider, get_default_provider, get_user_key
from app.core.config import settings
from app.core.llm_client import LLMError, call_gemini, call_openai_chat
from app.core.model_routing import resolve_provider
from app.models.agent import Agent
from app.services.usage_limits import check_and_record_usage

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

logger = logging.getLogger(__name__)

_provider_circuit: dict[str, dict[str, float]] = {}


@dataclass
class PlanStep:
    step_number: int
    action: str
    thought: str
    tool_name: str | None
    tool_candidates: list[str]
    tool_input: dict
    final_answer: str | None
    condition: dict | None
    else_step: "PlanStep | None"
    step_reason: str | None = None
    confidence: float | None = None


@dataclass
class PlanOutput:
    thought: str
    summary: str | None
    plan_reason: str | None
    plan_choice_confidence: float | None
    alternative_plans: list[str]
    steps: list[PlanStep]
    raw: str
    tokens: int
    cost_usd: float
    provider: str
    model: str


@dataclass
class RecoveryOutput:
    action: str
    thought: str
    tool_candidates: list[str]
    tool_input: dict
    final_answer: str | None
    raw: str
    tokens: int
    cost_usd: float


def generate_plan(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    memory_text: str,
    tools: list[str],
    steps_left: int,
    planner_hint: str | None = None,
) -> PlanOutput:
    provider, model = _get_provider_and_model(db, agent, user_id)
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        raise LLMError(f"Missing API key for {provider}")

    tool_list = ", ".join(tools) if tools else "(none)"
    system_prompt = (
        "You are an agent planner. Return ONLY valid JSON with this schema:\n"
        "{\n"
        '  "thought":"overall plan reasoning",\n'
        '  "summary":"short plan summary",\n'
        '  "plan_reason":"why this plan was chosen",\n'
        '  "plan_choice_confidence":0.0,\n'
        '  "alternative_plans":["optional"],\n'
        '  "plan":[\n'
        "    {\n"
        '      "step":1,\n'
        '      "action":"think|tool|final",\n'
        '      "thought":"step reasoning",\n'
        '      "step_reason":"why this step",\n'
        '      "confidence":0.0,\n'
        '      "tool_name":"optional",\n'
        '      "tool_candidates":["optional"],\n'
        '      "input":{},\n'
        '      "final_answer":"optional",\n'
        '      "condition": {"path":"last.output.field","op":"equals|contains|exists","value":"optional"},\n'
        '      "else": {"action":"think|tool|final","thought":"...","tool_name":"...","tool_candidates":["..."],"input":{},"final_answer":"..."}\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- action must be think, tool, or final.\n"
        "- If action=tool, include tool_name or tool_candidates and input.\n"
        "- If action=final, include final_answer.\n"
        "- condition is optional and only for branching after tool output.\n"
        "- Do not include extra keys or markdown.\n"
        "- Include plan_reason, plan_choice_confidence (0-1), and alternative_plans for the plan.\n"
        "- Include step_reason and confidence (0-1) for each plan step.\n"
        f"Available tools: {tool_list}\n"
        f"Steps left: {steps_left}\n"
    )
    if planner_hint:
        system_prompt += f"Planner guidance: {planner_hint}\n"

    user_prompt = "User request: " + (input_text or "(no input)")
    if memory_text:
        user_prompt += "\n\nContext:\n" + memory_text

    _ensure_provider_circuit(provider)
    try:
        raw, tokens = _call_llm(provider, model, api_key, system_prompt, user_prompt)
        _record_provider_success(provider)
    except LLMError:
        _record_provider_failure(provider)
        raise
    check_and_record_usage(db, user_id, provider, tokens)
    cost_usd = _estimate_cost(provider, model, tokens)

    parsed = _parse_planner_json(raw)
    if not parsed:
        logger.warning("planner_json_parse_failed")
        parsed = _repair_json(
            provider,
            model,
            api_key,
            raw,
            attempts=settings.agent_planner_json_repair_attempts,
        )

    if not parsed:
        return _fallback_plan("Planner output invalid. Please try again.", raw, tokens, cost_usd, provider, model)

    errors = _validate_plan_json(parsed)
    if errors:
        logger.warning("planner_json_invalid", extra={"errors": errors})
        parsed = _repair_json(
            provider,
            model,
            api_key,
            raw,
            attempts=settings.agent_planner_json_repair_attempts,
        )
        if parsed:
            errors = _validate_plan_json(parsed)
    if errors:
        return _fallback_plan("Planner output invalid. Please try again.", raw, tokens, cost_usd, provider, model)

    steps = _parse_plan_steps(parsed.get("plan"))
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else None
    thought = parsed.get("thought") if isinstance(parsed.get("thought"), str) else ""
    plan_reason = parsed.get("plan_reason") if isinstance(parsed.get("plan_reason"), str) else None
    plan_choice_confidence = parsed.get("plan_choice_confidence")
    if isinstance(plan_choice_confidence, (int, float)):
        plan_choice_confidence = float(plan_choice_confidence)
    else:
        plan_choice_confidence = None
    alternative_plans = parsed.get("alternative_plans") if isinstance(parsed.get("alternative_plans"), list) else []
    alternative_plans = [str(item) for item in alternative_plans if item]

    errors = _validate_plan(steps, tools)
    if errors:
        logger.warning("planner_plan_invalid", extra={"errors": errors})
        return _fallback_plan("Planner plan invalid. Please try again.", raw, tokens, cost_usd, provider, model)

    return PlanOutput(
        thought=thought,
        summary=summary,
        plan_reason=plan_reason,
        plan_choice_confidence=plan_choice_confidence,
        alternative_plans=alternative_plans,
        steps=steps,
        raw=raw,
        tokens=tokens,
        cost_usd=cost_usd,
        provider=provider,
        model=model,
    )


def plan_recovery_action(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    memory_text: str,
    tool_name: str,
    tool_input: dict,
    tool_error: str,
    tools: list[str],
) -> RecoveryOutput:
    provider, model = _get_provider_and_model(db, agent, user_id)
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        raise LLMError(f"Missing API key for {provider}")

    tool_list = ", ".join(tools) if tools else "(none)"
    system_prompt = (
        "You are an agent recovery planner. Return ONLY valid JSON with this schema:\n"
        '{"thought":"...","action":"retry|fallback|final","tool_candidates":["..."],"input":{},"final_answer":"..."}\n'
        "Rules:\n"
        "- action retry: keep same tool but modify input if needed.\n"
        "- action fallback: pick a different tool from candidates.\n"
        "- action final: provide final_answer if tooling cannot proceed.\n"
        "- For retry/fallback include input.\n"
        "- No extra keys or markdown.\n"
        f"Available tools: {tool_list}\n"
    )

    user_prompt = (
        "Original request: "
        + (input_text or "(no input)")
        + "\nTool failure:\n"
        + f"Tool: {tool_name}\n"
        + f"Input: {json.dumps(tool_input, ensure_ascii=True)}\n"
        + f"Error: {tool_error}\n"
    )
    if memory_text:
        user_prompt += "\nContext:\n" + memory_text

    _ensure_provider_circuit(provider)
    try:
        raw, tokens = _call_llm(provider, model, api_key, system_prompt, user_prompt)
        _record_provider_success(provider)
    except LLMError:
        _record_provider_failure(provider)
        raise
    check_and_record_usage(db, user_id, provider, tokens)
    cost_usd = _estimate_cost(provider, model, tokens)

    parsed = _parse_planner_json(raw) or {}
    action = str(parsed.get("action") or "").strip().lower()
    thought = str(parsed.get("thought") or "").strip()
    candidates = parsed.get("tool_candidates") if isinstance(parsed.get("tool_candidates"), list) else []
    tool_input = parsed.get("input") if isinstance(parsed.get("input"), dict) else {}
    final_answer = parsed.get("final_answer") if isinstance(parsed.get("final_answer"), str) else None

    if action not in {"retry", "fallback", "final"}:
        action = "final"
        final_answer = "Tool failed and recovery plan was invalid. Please try again."

    return RecoveryOutput(
        action=action,
        thought=thought,
        tool_candidates=[str(item) for item in candidates if item],
        tool_input=tool_input,
        final_answer=final_answer,
        raw=raw,
        tokens=tokens,
        cost_usd=cost_usd,
    )


def summarize_run_context(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    memory_text: str,
) -> tuple[str, int, float]:
    provider, model = _get_provider_and_model(db, agent, user_id)
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        raise LLMError(f"Missing API key for {provider}")

    system_prompt = (
        "You summarize agent run context into a short memory.\n"
        "Return ONLY plain text, max 5 bullet points, no markdown headers.\n"
        "Focus on goals, decisions, tools used, and key outputs."
    )
    user_prompt = "User request: " + (input_text or "(no input)")
    if memory_text:
        user_prompt += "\n\nContext:\n" + memory_text

    _ensure_provider_circuit(provider)
    try:
        raw, tokens = _call_llm(provider, model, api_key, system_prompt, user_prompt)
        _record_provider_success(provider)
    except LLMError:
        _record_provider_failure(provider)
        raise
    check_and_record_usage(db, user_id, provider, tokens)
    cost_usd = _estimate_cost(provider, model, tokens)
    return raw.strip(), tokens, cost_usd


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


def _validate_plan_json(payload: dict) -> list[str]:
    errors: list[str] = []
    allowed_top = {"thought", "summary", "plan", "plan_reason", "plan_choice_confidence", "alternative_plans"}
    extra_top = set(payload.keys()) - allowed_top
    if extra_top:
        errors.append(f"Unexpected keys: {sorted(extra_top)}")
    if "plan" not in payload or not isinstance(payload.get("plan"), list):
        errors.append("Missing or invalid plan list")
        return errors
    if "plan_choice_confidence" in payload and payload.get("plan_choice_confidence") is not None and not isinstance(
        payload.get("plan_choice_confidence"), (int, float)
    ):
        errors.append("plan_choice_confidence must be a number")
    if "alternative_plans" in payload and payload.get("alternative_plans") is not None and not isinstance(
        payload.get("alternative_plans"), list
    ):
        errors.append("alternative_plans must be a list")

    for step in payload.get("plan"):
        if not isinstance(step, dict):
            errors.append("Plan step is not an object")
            continue
        allowed_step = {
            "step",
            "action",
            "thought",
            "tool_name",
            "tool_candidates",
            "input",
            "final_answer",
            "condition",
            "else",
            "step_reason",
            "confidence",
        }
        extra_step = set(step.keys()) - allowed_step
        if extra_step:
            errors.append(f"Unexpected step keys: {sorted(extra_step)}")
        if not isinstance(step.get("action"), str):
            errors.append("Step action must be a string")
        if "input" in step and step.get("input") is not None and not isinstance(step.get("input"), dict):
            errors.append("Step input must be an object")
        if "tool_candidates" in step and step.get("tool_candidates") is not None and not isinstance(
            step.get("tool_candidates"), list
        ):
            errors.append("tool_candidates must be a list")
        if "confidence" in step and step.get("confidence") is not None and not isinstance(
            step.get("confidence"), (int, float)
        ):
            errors.append("confidence must be a number")
        if "condition" in step and step.get("condition") is not None and not isinstance(
            step.get("condition"), dict
        ):
            errors.append("condition must be an object")
        if "else" in step and step.get("else") is not None and not isinstance(step.get("else"), dict):
            errors.append("else must be an object")

    return errors


def _repair_json(
    provider: str,
    model: str,
    api_key: str,
    raw: str,
    attempts: int,
) -> dict | None:
    if attempts <= 0:
        return None

    system_prompt = (
        "You are a JSON repair bot. Return ONLY valid JSON."
        "Given a broken JSON, output a corrected JSON that matches the expected schema."
    )
    user_prompt = "Repair this JSON output:\n" + raw

    for _ in range(attempts):
        fixed_raw, _tokens = _call_llm(provider, model, api_key, system_prompt, user_prompt)
        parsed = _parse_planner_json(fixed_raw)
        if parsed:
            return parsed
    return None


def _ensure_provider_circuit(provider: str) -> None:
    state = _provider_circuit.get(provider)
    if not state:
        return
    open_until = state.get("open_until", 0)
    if time.monotonic() < open_until:
        raise LLMError("Provider circuit breaker open")


def _record_provider_failure(provider: str) -> None:
    failures = _provider_circuit.get(provider, {}).get("failures", 0) + 1
    state = _provider_circuit.setdefault(provider, {})
    state["failures"] = failures
    if failures >= settings.agent_provider_circuit_breaker_failures:
        state["open_until"] = time.monotonic() + settings.agent_provider_circuit_cooldown_seconds


def _record_provider_success(provider: str) -> None:
    if provider in _provider_circuit:
        _provider_circuit.pop(provider, None)


def _parse_plan_steps(raw_steps: object) -> list[PlanStep]:
    if not isinstance(raw_steps, list):
        return []
    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            continue
        action = str(raw_step.get("action", "")).strip().lower()
        tool_name = raw_step.get("tool_name")
        tool_candidates = raw_step.get("tool_candidates") if isinstance(raw_step.get("tool_candidates"), list) else []
        tool_input = raw_step.get("input") if isinstance(raw_step.get("input"), dict) else {}
        final_answer = raw_step.get("final_answer") if isinstance(raw_step.get("final_answer"), str) else None
        thought = raw_step.get("thought") if isinstance(raw_step.get("thought"), str) else ""
        step_reason = raw_step.get("step_reason") if isinstance(raw_step.get("step_reason"), str) else None
        confidence = raw_step.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence = float(confidence)
        else:
            confidence = None
        condition = raw_step.get("condition") if isinstance(raw_step.get("condition"), dict) else None
        else_raw = raw_step.get("else") if isinstance(raw_step.get("else"), dict) else None
        else_step = None
        if else_raw:
            else_step = PlanStep(
                step_number=0,
                action=str(else_raw.get("action", "")).strip().lower(),
                thought=str(else_raw.get("thought", "")),
                tool_name=else_raw.get("tool_name"),
                tool_candidates=else_raw.get("tool_candidates")
                if isinstance(else_raw.get("tool_candidates"), list)
                else [],
                tool_input=else_raw.get("input") if isinstance(else_raw.get("input"), dict) else {},
                final_answer=else_raw.get("final_answer")
                if isinstance(else_raw.get("final_answer"), str)
                else None,
                condition=None,
                else_step=None,
            )
        steps.append(
            PlanStep(
                step_number=int(raw_step.get("step") or index),
                action=action,
                thought=thought,
                tool_name=str(tool_name) if tool_name else None,
                tool_candidates=[str(item) for item in tool_candidates if item],
                tool_input=tool_input,
                final_answer=final_answer,
                condition=condition,
                else_step=else_step,
                step_reason=step_reason,
                confidence=confidence,
            )
        )
    return steps


def _validate_plan(steps: list[PlanStep], tools: list[str]) -> list[str]:
    errors: list[str] = []
    if not steps:
        return ["Plan has no steps"]
    for step in steps:
        if step.action not in {"think", "tool", "final"}:
            errors.append(f"Invalid action: {step.action}")
        if step.action == "tool":
            if not step.tool_name and not step.tool_candidates:
                errors.append("Tool action missing tool name or candidates")
            for candidate in step.tool_candidates:
                if candidate not in tools:
                    errors.append(f"Unknown tool candidate: {candidate}")
            if step.tool_name and step.tool_name not in tools:
                errors.append(f"Unknown tool: {step.tool_name}")
        if step.action == "final" and not step.final_answer:
            errors.append("Final action missing final_answer")
    return errors


def _fallback_plan(
    message: str,
    raw: str,
    tokens: int,
    cost_usd: float,
    provider: str,
    model: str,
) -> PlanOutput:
    return PlanOutput(
        thought="",
        summary=None,
        plan_reason=None,
        plan_choice_confidence=None,
        alternative_plans=[],
        steps=[
            PlanStep(
                step_number=1,
                action="final",
                thought="",
                tool_name=None,
                tool_candidates=[],
                tool_input={},
                final_answer=message,
                condition=None,
                else_step=None,
                step_reason=None,
                confidence=None,
            )
        ],
        raw=raw,
        tokens=tokens,
        cost_usd=cost_usd,
        provider=provider,
        model=model,
    )


def _call_llm(
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, int]:
    if provider == "openai":
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return call_openai_chat(api_key, model, messages)
    prompt = system_prompt + "\n" + user_prompt
    return call_gemini(api_key, model, prompt)


def _estimate_cost(provider: str, model: str, tokens: int) -> float:
    pricing = {
        "openai": {
            "gpt-4o-mini": 0.00015,
        },
        "gemini": {
            "gemini-2.5-flash": 0.0001,
        },
    }
    default_rate = 0.0001
    per_1k = pricing.get(provider, {}).get(model, default_rate)
    return round((tokens / 1000.0) * per_1k, 6)


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
