import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ai_keys import get_code_provider, get_default_provider, get_user_key
from app.core.llm_client import LLMError, call_gemini, call_openai_chat, serialize_messages
from app.core.model_routing import resolve_provider
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.services.usage_limits import check_and_record_usage
from app.api.routes import tools as tool_routes

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"


class AgentRuntimeError(RuntimeError):
    pass


def _get_provider_and_model(db: Session, agent: Agent, user_id: int) -> tuple[str, str]:
    if agent.model != "auto":
        provider = "openai" if agent.model.startswith("gpt-") else "gemini"
        return provider, agent.model

    default_provider = get_default_provider(db)
    code_provider = get_code_provider(db)
    provider = resolve_provider(db, user_id, agent.role, agent.category, default_provider, code_provider)
    if not provider:
        raise AgentRuntimeError("No model provider available")
    model = OPENAI_DEFAULT_MODEL if provider == "openai" else GEMINI_DEFAULT_MODEL
    return provider, model


def _create_step(
    db: Session,
    run_id: int,
    step_index: int,
    kind: str,
    status: str,
    content: dict,
) -> None:
    db.add(
        AgentRunStep(
            run_id=run_id,
            step_index=step_index,
            kind=kind,
            status=status,
            content=json.dumps(content, ensure_ascii=True),
        )
    )


def execute_agent_run(
    db: Session,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    source: str,
) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id,
        user_id=user_id,
        status="running",
        input_text=input_text,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    plan_text = (
        f"Plan for {agent.name}: understand the request, decide tools, produce response."
        if input_text
        else f"Plan for {agent.name}: run default workflow."
    )
    _create_step(
        db,
        run.id,
        1,
        "plan",
        "completed",
        {"plan": plan_text, "source": source},
    )

    tools = json.loads(agent.tools or "[]")
    tool_call = None
    if input_text:
        stripped = input_text.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
                if isinstance(payload, dict) and payload.get("tool"):
                    tool_call = {
                        "name": payload.get("tool"),
                        "arguments": payload.get("arguments") or {},
                    }
            except json.JSONDecodeError:
                tool_call = None
        if stripped.lower().startswith("tool:"):
            parts = stripped.split(" ", 2)
            if len(parts) >= 2:
                tool_call = {"name": parts[1].strip(), "arguments": {}}

    if tools:
        if tool_call and tool_call.get("name") in tools:
            _create_step(
                db,
                run.id,
                2,
                "tool",
                "running",
                {"tool": tool_call.get("name")},
            )
        else:
            _create_step(
                db,
                run.id,
                2,
                "tool",
                "skipped",
                {"reason": "No tool call found", "tools": tools},
            )
    else:
        _create_step(
            db,
            run.id,
            2,
            "tool",
            "skipped",
            {"reason": "No tools configured"},
        )

    try:
        if tool_call and tool_call.get("name") in tools:
            tool_args = tool_call.get("arguments") or {}
            tool_args["user_id"] = user_id
            tool_args["agent_id"] = agent.id
            result = _execute_tool_call(db, tool_call.get("name"), tool_args)
            _create_step(db, run.id, 3, "tool_result", "completed", result)
            run.status = "completed"
            run.output_text = json.dumps(result, ensure_ascii=True)
        else:
            provider, model = _get_provider_and_model(db, agent, user_id)
            api_key = get_user_key(db, user_id, provider)
            if not api_key:
                raise AgentRuntimeError(f"Missing API key for {provider}")

            system_prompt = (
                f"You are {agent.name}. Role: {agent.role}. Provide concise, actionable output."
            )
            prompt = input_text or "Run the default task."

            if provider == "openai":
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                output_text, tokens = call_openai_chat(api_key, model, messages)
                payload = {
                    "provider": provider,
                    "model": model,
                    "messages": serialize_messages(messages),
                    "output": output_text,
                }
            else:
                prompt_text = f"{system_prompt}\n\nUser: {prompt}"
                output_text, tokens = call_gemini(api_key, model, prompt_text)
                payload = {
                    "provider": provider,
                    "model": model,
                    "prompt": prompt_text,
                    "output": output_text,
                }

            check_and_record_usage(db, user_id, provider, tokens)
            _create_step(db, run.id, 3, "result", "completed", payload)

            run.status = "completed"
            run.output_text = output_text
    except (LLMError, AgentRuntimeError, ValueError) as exc:
        _create_step(db, run.id, 3, "error", "failed", {"error": str(exc)})
        run.status = "failed"
        run.output_text = str(exc)

    db.add(run)
    db.commit()
    db.refresh(run)

    return run


def _execute_tool_call(db: Session, name: str, args: dict) -> dict:
    if name == "gmail.draft":
        return tool_routes._gmail_draft(args, db, int(args["user_id"]))
    if name == "gmail.send_request":
        return tool_routes._gmail_send_request(
            args, db, int(args["user_id"]), int(args.get("agent_id")) if args.get("agent_id") else None
        )
    if name == "gmail.send":
        return tool_routes._gmail_send(args, db, int(args["user_id"]))
    if name == "gmail.profile":
        return tool_routes._gmail_profile(db, int(args["user_id"]))
    if name == "calendar.list":
        return tool_routes._calendar_list(db, int(args["user_id"]))
    if name == "calendar.create_request":
        return tool_routes._calendar_create_request(
            args, db, int(args["user_id"]), int(args.get("agent_id")) if args.get("agent_id") else None
        )
    if name == "gmail.list_messages":
        return tool_routes._gmail_list_messages(args, db, int(args["user_id"]))
    if name == "gmail.list_drafts":
        return tool_routes._gmail_list_drafts(args, db, int(args["user_id"]))
    raise AgentRuntimeError(f"Unknown tool: {name}")
