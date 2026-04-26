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
from app.services.agent_analytics import get_planner_guidance
from app.services.agent_memory import build_memory_context, get_recent_steps
from app.services.agent_planner import generate_plan, plan_recovery_action, summarize_run_context
from app.services.agent_state import add_step, create_run, finalize_run
from app.services.agent_tool_router import rank_tools
from app.services.feature_flags import get_bool, get_mode
from app.services.dry_run_log import record_shadow_dry_run
from app.services.hitl_queue import record_shadow_confirmation
from app.services.intent_verifier import evaluate as evaluate_intent
from app.services.tool_call_audit import now_ms, record_tool_call, should_audit
from app.services.validation_kernel import evaluate as evaluate_validation
from app.services.safety_kernel import evaluate as evaluate_safety
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


def _execute_tool_with_audit(
    db: Session,
    *,
    tool_name: str,
    tool_args: dict,
    user_id: int,
    agent_id: int | None,
    run_id: int | None,
    step_index: int | None,
    user_text: str | None,
    step_thought: str | None,
    direct_call: bool,
) -> dict:
    validation_mode = get_mode("validation_kernel_mode")
    intent_mode = get_mode("intent_verifier_mode")
    safety_mode = get_mode("safety_kernel_mode")
    risk_registry_enabled = get_bool("risk_registry_enabled")
    hitl_enabled = get_bool("hitl_enabled")
    dry_run_enabled = get_bool("dry_run_enabled")
    audit_enabled = should_audit(validation_mode, intent_mode) or safety_mode != "off"

    kernel_decisions: dict = {}
    if validation_mode in {"shadow", "enforce"}:
        decision = evaluate_validation(tool_name, tool_args)
        kernel_decisions["validation"] = decision.status
        kernel_decisions["validation_reasons"] = decision.reasons

    if intent_mode in {"shadow", "enforce"}:
        decision = evaluate_intent(
            user_text,
            tool_name,
            direct_call=direct_call,
            step_thought=step_thought,
        )
        kernel_decisions["intent"] = decision.status
        kernel_decisions["intent_reasons"] = decision.reasons

    hitl_required = False
    dry_run_required = False
    if safety_mode in {"shadow", "enforce"} and risk_registry_enabled:
        decision = evaluate_safety(db, tool_name)
        kernel_decisions["safety"] = decision.status
        kernel_decisions["risk_tier"] = decision.risk_tier
        kernel_decisions["safety_reasons"] = decision.reasons
        hitl_required = decision.requires_hitl
        dry_run_required = decision.requires_dry_run
        kernel_decisions["requires_dry_run"] = dry_run_required

    if hitl_enabled and hitl_required:
        record_shadow_confirmation(
            db,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            step_index=step_index,
            tool_name=tool_name,
            args=tool_args,
            reason="HITL required (shadow mode)",
        )

    start = time.monotonic() if audit_enabled else None
    try:
        result = execute_tool(db, tool_name, tool_args, user_id, agent_id, retries=1)
        if dry_run_enabled and dry_run_required:
            record_shadow_dry_run(
                db,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                step_index=step_index,
                tool_name=tool_name,
                args=tool_args,
                reason="Dry-run required (shadow mode)",
            )
        if audit_enabled:
            record_tool_call(
                db,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                step_index=step_index,
                tool_name=tool_name,
                tool_category=None,
                source="agent",
                mode="live",
                args=tool_args,
                result=result if isinstance(result, dict) else {"result": result},
                status="ok",
                error_class=None,
                error_message=None,
                latency_ms=now_ms(start),
                kernel_decisions=kernel_decisions,
                hitl_required=hitl_required,
            )
        return result
    except ToolExecutionError as exc:
        if audit_enabled:
            record_tool_call(
                db,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                step_index=step_index,
                tool_name=tool_name,
                tool_category=None,
                source="agent",
                mode="live",
                args=tool_args,
                result=None,
                status="error",
                error_class=exc.__class__.__name__,
                error_message=str(exc),
                latency_ms=now_ms(start),
                kernel_decisions=kernel_decisions,
                hitl_required=hitl_required,
            )
        raise


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

    if run.status in {"completed", "failed"}:
        logger.info("agent_run_skip", extra={"run_id": run.id, "status": run.status})
        return run

    if run.status == "running":
        logger.info("agent_run_already_running", extra={"run_id": run.id})
        return run

    agent = db.query(Agent).filter(Agent.id == run.agent_id).one_or_none()
    if not agent:
        raise AgentRuntimeError("Agent not found")

    updated = (
        db.query(AgentRun)
        .filter(AgentRun.id == run_id, AgentRun.status == "pending")
        .update({"status": "running", "started_at": datetime.now(timezone.utc)})
    )
    db.commit()
    if updated == 0:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).one_or_none()
        if not run or run.status in {"completed", "failed", "running"}:
            logger.info("agent_run_state_conflict", extra={"run_id": run_id})
            return run
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
    summary_every = settings.agent_summary_every
    tool_retry_limit = settings.agent_tool_retry_limit
    tool_retry_backoff = settings.agent_tool_retry_backoff_seconds
    tool_retry_backoff_max = settings.agent_tool_retry_max_backoff_seconds
    tool_kill_switch_seconds = settings.agent_tool_kill_switch_seconds
    max_execution_depth = settings.agent_max_execution_depth
    max_plan_cycles = settings.agent_max_plan_cycles
    max_tool_failures = settings.agent_max_tool_failures
    max_tool_retries = settings.agent_max_tool_retries_per_run
    max_tokens = settings.agent_max_tokens
    max_cost = settings.agent_max_cost_usd
    cost_warning_ratio = settings.agent_cost_warning_ratio
    token_warning_ratio = settings.agent_token_warning_ratio
    start_time = time.monotonic()
    total_tokens = int(run.total_tokens or 0)
    total_cost = float(run.total_cost_usd or 0.0)

    plan = None
    plan_index = 0
    last_tool_output: dict | None = None
    plan_cycle_counts: dict[str, int] = {}
    tool_failure_counts: dict[str, int] = {}
    tool_retry_count = 0
    warned_cost = False
    warned_tokens = False

    logger.info("agent_run_start", extra={"run_id": run.id, "agent_id": agent.id, "source": source})

    direct_tool = _parse_direct_tool_call(input_text)
    if direct_tool and direct_tool.get("name") in tools:
        tool_args = direct_tool.get("arguments") or {}
        direct_reasoning = {
            "selected_tool_reason": "Direct tool invocation requested by user input.",
            "rejected_tools_reason": [],
            "scoring_breakdown": {},
        }
        try:
            start_tool = time.monotonic()
            result = _execute_tool_with_audit(
                db,
                tool_name=direct_tool["name"],
                tool_args=tool_args,
                user_id=user_id,
                agent_id=agent.id,
                run_id=run.id,
                step_index=1,
                user_text=input_text,
                step_thought=None,
                direct_call=True,
            )
            tool_elapsed = time.monotonic() - start_tool
            if tool_elapsed > tool_kill_switch_seconds:
                raise ToolExecutionError("Tool execution exceeded kill switch")
            add_step(
                db,
                run.id,
                1,
                "tool",
                "success",
                tool_name=direct_tool["name"],
                input_data=tool_args,
                output_data=result,
                reasoning={
                    **direct_reasoning,
                    "success_reason": "Tool executed successfully via direct invocation.",
                },
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
                reasoning={
                    **direct_reasoning,
                    "failure_root_cause": str(exc),
                },
            )
            finalize_run(db, run, "failed", str(exc), str(exc))
            return run

    step_number = 1
    while step_number <= max_steps:
        if step_number > max_execution_depth:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": "Max execution depth reached"},
                reasoning={"failure_root_cause": "Max execution depth reached"},
            )
            finalize_run(db, run, "failed", "Max execution depth reached", "Max execution depth reached")
            logger.info("agent_run_depth_limit", extra={"run_id": run.id})
            return run
        if time.monotonic() - start_time > timeout_seconds:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": "Run timed out"},
                reasoning={"failure_root_cause": "Run timed out"},
            )
            finalize_run(db, run, "failed", "Run timed out", "Run timed out")
            logger.info("agent_run_timeout", extra={"run_id": run.id})
            return run

        if total_tokens >= max_tokens:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": "Token limit exceeded"},
                reasoning={"failure_root_cause": "Token limit exceeded"},
            )
            finalize_run(db, run, "failed", "Token limit exceeded", "Token limit exceeded")
            logger.info("agent_run_token_limit", extra={"run_id": run.id})
            return run

        if total_cost >= max_cost:
            add_step(
                db,
                run.id,
                step_number,
                "error",
                "failed",
                output_data={"error": "Cost limit exceeded"},
                reasoning={"failure_root_cause": "Cost limit exceeded"},
            )
            finalize_run(db, run, "failed", "Cost limit exceeded", "Cost limit exceeded")
            logger.info("agent_run_cost_limit", extra={"run_id": run.id})
            return run

        if not warned_tokens and total_tokens >= int(max_tokens * token_warning_ratio):
            add_step(
                db,
                run.id,
                step_number,
                "warning",
                "success",
                output_data={"warning": "Token usage approaching limit"},
                reasoning={"success_reason": "Token usage warning emitted"},
            )
            warned_tokens = True
            step_number += 1
            continue

        if not warned_cost and total_cost >= max_cost * cost_warning_ratio:
            add_step(
                db,
                run.id,
                step_number,
                "warning",
                "success",
                output_data={"warning": "Cost usage approaching limit"},
                reasoning={"success_reason": "Cost usage warning emitted"},
            )
            warned_cost = True
            step_number += 1
            continue

        recent = get_recent_steps(db, run.id, memory_steps) if memory_steps > 0 else []
        memory_text = build_memory_context(run.summary_memory, recent)

        if not plan or plan_index >= len(plan.steps):
            try:
                steps_left = max_steps - step_number + 1
                guidance = get_planner_guidance(db, user_id, agent.id, steps_left)
                plan = generate_plan(
                    db,
                    agent,
                    user_id,
                    input_text,
                    memory_text,
                    tools,
                    guidance.adjusted_steps_left,
                    guidance.prompt,
                )
            except (LLMError, HTTPException, ValueError) as exc:
                add_step(
                    db,
                    run.id,
                    step_number,
                    "error",
                    "failed",
                    output_data={"error": str(exc)},
                    reasoning={"failure_root_cause": str(exc)},
                )
                finalize_run(db, run, "failed", str(exc), str(exc))
                return run

            total_tokens, total_cost = _update_usage(db, run, total_tokens, total_cost, plan.tokens, plan.cost_usd)
            if _usage_limits_exceeded(total_tokens, total_cost):
                add_step(
                    db,
                    run.id,
                    step_number,
                    "error",
                    "failed",
                    output_data={"error": "Cost or token limit exceeded"},
                    reasoning={"failure_root_cause": "Cost or token limit exceeded"},
                )
                finalize_run(db, run, "failed", "Cost or token limit exceeded", "Cost or token limit exceeded")
                logger.info("agent_run_usage_limit", extra={"run_id": run.id})
                return run
            plan_signature = _plan_signature(plan)
            plan_cycle_counts[plan_signature] = plan_cycle_counts.get(plan_signature, 0) + 1
            if plan_cycle_counts[plan_signature] > max_plan_cycles:
                add_step(
                    db,
                    run.id,
                    step_number,
                    "error",
                    "failed",
                    output_data={"error": "Repeated plan cycle detected"},
                    reasoning={"failure_root_cause": "Repeated plan cycle detected"},
                )
                finalize_run(db, run, "failed", "Repeated plan cycle detected", "Repeated plan cycle detected")
                logger.info("agent_run_plan_cycle", extra={"run_id": run.id})
                return run
            add_step(
                db,
                run.id,
                step_number,
                "plan",
                "success",
                thought=plan.summary or plan.thought,
                output_data={
                    "steps": [
                        {
                            "step": step.step_number,
                            "action": step.action,
                            "tool": step.tool_name or step.tool_candidates,
                            "condition": step.condition,
                            "step_reason": step.step_reason,
                            "confidence": step.confidence,
                        }
                        for step in plan.steps
                    ],
                    "plan_reason": plan.plan_reason,
                    "plan_choice_confidence": plan.plan_choice_confidence,
                    "alternative_plans": plan.alternative_plans,
                },
                tokens_used=plan.tokens,
                cost_usd=plan.cost_usd,
                reasoning={
                    "plan_step_reason": plan.plan_reason or plan.summary or plan.thought,
                    "plan_choice_confidence": plan.plan_choice_confidence,
                    "alternative_plans": plan.alternative_plans,
                },
            )
            step_number += 1
            plan_index = 0
            continue

        step = plan.steps[plan_index]
        plan_index += 1

        step_to_execute = _resolve_condition(step, last_tool_output)
        if step_to_execute is None:
            continue

        action = step_to_execute.action
        if action == "think":
            add_step(
                db,
                run.id,
                step_number,
                "think",
                "success",
                thought=step_to_execute.thought,
                reasoning={"success_reason": "Thought step recorded"},
            )
            step_number, total_tokens, total_cost = _maybe_summarize(
                db,
                run,
                agent,
                user_id,
                input_text,
                memory_text,
                step_number + 1,
                summary_every,
                total_tokens,
                total_cost,
            )
            continue

        if action == "final":
            add_step(
                db,
                run.id,
                step_number,
                "final",
                "success",
                thought=step_to_execute.thought,
                output_data={"final_answer": step_to_execute.final_answer},
                reasoning={"success_reason": "Final answer produced"},
            )
            finalize_run(db, run, "completed", step_to_execute.final_answer or "", None)
            logger.info("agent_run_completed", extra={"run_id": run.id, "steps": step_number})
            return run

        if action == "tool":
            tool_candidates = step_to_execute.tool_candidates or []
            if step_to_execute.tool_name:
                tool_candidates = [step_to_execute.tool_name] + tool_candidates

            context_text = "\n".join(filter(None, [input_text or "", memory_text, step_to_execute.thought]))
            ranked = rank_tools(db, user_id, agent.id, tools, context_text, recent, tool_candidates or None)
            if not ranked:
                add_step(
                    db,
                    run.id,
                    step_number,
                    "error",
                    "failed",
                    thought=step_to_execute.thought,
                    output_data={"error": "Tool not available"},
                    reasoning={"failure_root_cause": "Tool not available"},
                )
                finalize_run(db, run, "failed", "Tool not available", "Tool not available")
                logger.info("agent_run_tool_missing", extra={"run_id": run.id})
                return run

            selected_tool = ranked[0].tool
            scoring_breakdown = {item.tool: item.breakdown for item in ranked}
            selected_reason = ranked[0].reasons
            rejected_reasons = [
                {"tool": item.tool, "reasons": item.reasons}
                for item in ranked[1:]
            ]
            tool_args = step_to_execute.tool_input or {}

            attempt = 0
            while attempt <= tool_retry_limit:
                try:
                    start_tool = time.monotonic()
                    result = _execute_tool_with_audit(
                        db,
                        tool_name=selected_tool,
                        tool_args=tool_args,
                        user_id=user_id,
                        agent_id=agent.id,
                        run_id=run.id,
                        step_index=step_number,
                        user_text=input_text,
                        step_thought=step_to_execute.thought,
                        direct_call=False,
                    )
                    tool_elapsed = time.monotonic() - start_tool
                    if tool_elapsed > tool_kill_switch_seconds:
                        raise ToolExecutionError("Tool execution exceeded kill switch")
                    status = "success" if attempt == 0 else "retry"
                    add_step(
                        db,
                        run.id,
                        step_number,
                        "tool",
                        status,
                        thought=step_to_execute.thought,
                        tool_name=selected_tool,
                        input_data=tool_args,
                        output_data=result,
                        reasoning={
                            "selected_tool_reason": selected_reason,
                            "rejected_tools_reason": rejected_reasons,
                            "scoring_breakdown": scoring_breakdown,
                            "success_reason": "Tool completed successfully.",
                        },
                    )
                    last_tool_output = result if isinstance(result, dict) else {"result": result}
                    logger.info(
                        "agent_tool_success",
                        extra={"run_id": run.id, "tool": selected_tool, "step": step_number},
                    )
                    step_number, total_tokens, total_cost = _maybe_summarize(
                        db,
                        run,
                        agent,
                        user_id,
                        input_text,
                        memory_text,
                        step_number + 1,
                        summary_every,
                        total_tokens,
                        total_cost,
                    )
                    break
                except ToolExecutionError as exc:
                    tool_failure_counts[selected_tool] = tool_failure_counts.get(selected_tool, 0) + 1
                    if tool_failure_counts[selected_tool] > max_tool_failures:
                        add_step(
                            db,
                            run.id,
                            step_number,
                            "error",
                            "failed",
                            output_data={"error": "Repeated tool failures"},
                            reasoning={"failure_root_cause": "Repeated tool failures"},
                        )
                        finalize_run(db, run, "failed", "Repeated tool failures", "Repeated tool failures")
                        logger.info("agent_run_tool_failures", extra={"run_id": run.id, "tool": selected_tool})
                        return run
                    add_step(
                        db,
                        run.id,
                        step_number,
                        "tool",
                        "failed",
                        thought=step_to_execute.thought,
                        tool_name=selected_tool,
                        input_data=tool_args,
                        output_data={"error": str(exc)},
                        reasoning={
                            "selected_tool_reason": selected_reason,
                            "rejected_tools_reason": rejected_reasons,
                            "scoring_breakdown": scoring_breakdown,
                            "failure_root_cause": str(exc),
                        },
                    )
                    attempt += 1
                    tool_retry_count += 1
                    if tool_retry_count > max_tool_retries:
                        finalize_run(db, run, "failed", "Too many tool retries", "Too many tool retries")
                        logger.info("agent_run_retry_limit", extra={"run_id": run.id})
                        return run
                    if attempt > tool_retry_limit:
                        finalize_run(db, run, "failed", str(exc), str(exc))
                        return run

                    backoff = min(tool_retry_backoff * (2 ** (attempt - 1)), tool_retry_backoff_max)
                    time.sleep(backoff)

                    try:
                        recovery = plan_recovery_action(
                            db,
                            agent,
                            user_id,
                            input_text,
                            memory_text,
                            selected_tool,
                            tool_args,
                            str(exc),
                            tools,
                        )
                    except (LLMError, HTTPException, ValueError) as recovery_exc:
                        finalize_run(db, run, "failed", str(recovery_exc), str(recovery_exc))
                        return run

                    total_tokens, total_cost = _update_usage(
                        db, run, total_tokens, total_cost, recovery.tokens, recovery.cost_usd
                    )
                    if _usage_limits_exceeded(total_tokens, total_cost):
                        add_step(
                            db,
                            run.id,
                            step_number,
                            "error",
                            "failed",
                            output_data={"error": "Cost or token limit exceeded"},
                            reasoning={"failure_root_cause": "Cost or token limit exceeded"},
                        )
                        finalize_run(db, run, "failed", "Cost or token limit exceeded", "Cost or token limit exceeded")
                        logger.info("agent_run_usage_limit", extra={"run_id": run.id})
                        return run
                    add_step(
                        db,
                        run.id,
                        step_number,
                        "recovery",
                        "success",
                        thought=recovery.thought,
                        output_data={
                            "action": recovery.action,
                            "tool_candidates": recovery.tool_candidates,
                        },
                        tokens_used=recovery.tokens,
                        cost_usd=recovery.cost_usd,
                        reasoning={
                            "retry_reason": str(exc),
                            "fallback_strategy_used": recovery.action,
                        },
                    )
                    step_number += 1

                    if recovery.action == "final":
                        finalize_run(db, run, "failed", recovery.final_answer or "", recovery.final_answer)
                        return run

                    if recovery.tool_candidates:
                        ranked = rank_tools(
                            db,
                            user_id,
                            agent.id,
                            tools,
                            context_text,
                            recent,
                            recovery.tool_candidates,
                        )
                        if ranked:
                            selected_tool = ranked[0].tool
                            scoring_breakdown = {item.tool: item.breakdown for item in ranked}
                            selected_reason = ranked[0].reasons
                            rejected_reasons = [
                                {"tool": item.tool, "reasons": item.reasons}
                                for item in ranked[1:]
                            ]
                    if recovery.tool_input:
                        tool_args = recovery.tool_input

        step_number += 1

    add_step(
        db,
        run.id,
        step_number,
        "error",
        "failed",
        output_data={"error": "Max steps reached"},
        reasoning={"failure_root_cause": "Max steps reached"},
    )
    finalize_run(db, run, "failed", "Max steps reached", "Max steps reached")
    logger.info("agent_run_max_steps", extra={"run_id": run.id})
    return run


def _update_usage(
    db: Session,
    run: AgentRun,
    total_tokens: int,
    total_cost: float,
    tokens: int,
    cost_usd: float,
) -> tuple[int, float]:
    total_tokens += int(tokens or 0)
    total_cost += float(cost_usd or 0.0)
    run.total_tokens = total_tokens
    run.total_cost_usd = total_cost
    db.add(run)
    db.commit()
    db.refresh(run)
    return total_tokens, total_cost


def _usage_limits_exceeded(total_tokens: int, total_cost: float) -> bool:
    if total_tokens >= settings.agent_max_tokens:
        return True
    if total_cost >= settings.agent_max_cost_usd:
        return True
    return False


def _plan_signature(plan) -> str:
    if not plan or not plan.steps:
        return "empty"
    parts = []
    for step in plan.steps:
        condition = step.condition or {}
        parts.append(
            "|".join(
                [
                    step.action,
                    step.tool_name or "",
                    ",".join(step.tool_candidates or []),
                    str(condition.get("path") or ""),
                    str(condition.get("op") or ""),
                ]
            )
        )
    return "::".join(parts)


def _resolve_condition(step, last_tool_output: dict | None):
    if not step.condition:
        return step
    if not last_tool_output:
        return step.else_step
    if _evaluate_condition(step.condition, last_tool_output):
        return step
    return step.else_step


def _evaluate_condition(condition: dict, last_tool_output: dict) -> bool:
    path = condition.get("path")
    op = str(condition.get("op") or "").lower()
    value = condition.get("value")
    if not path:
        return False
    target = _extract_path(last_tool_output, path)
    if op == "exists":
        return target is not None
    if op == "equals":
        return target == value
    if op == "contains":
        if isinstance(target, (list, tuple, set)):
            return value in target
        if isinstance(target, str):
            return str(value) in target
    return False


def _extract_path(payload: dict, path: str):
    current = payload
    parts = [part for part in path.replace("last.", "").split(".") if part]
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _maybe_summarize(
    db: Session,
    run: AgentRun,
    agent: Agent,
    user_id: int,
    input_text: str | None,
    memory_text: str,
    next_step: int,
    summary_every: int,
    total_tokens: int,
    total_cost: float,
) -> tuple[int, int, float]:
    if summary_every <= 0 or next_step % summary_every != 0:
        return next_step, total_tokens, total_cost

    try:
        summary, tokens, cost = summarize_run_context(
            db,
            agent,
            user_id,
            input_text,
            memory_text,
        )
    except (LLMError, HTTPException, ValueError):
        return next_step, total_tokens, total_cost

    run.summary_memory = summary
    total_tokens, total_cost = _update_usage(db, run, total_tokens, total_cost, tokens, cost)

    add_step(
        db,
        run.id,
        next_step,
        "memory",
        "success",
        thought=summary,
        tokens_used=tokens,
        cost_usd=cost,
    )
    return next_step + 1, total_tokens, total_cost
