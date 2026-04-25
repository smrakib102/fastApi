"""WorkflowEngine — Phase 6.

Executes a small, deterministic DAG of steps. Each step is one of:

  * ``tool``  — invoke a tool by name (resolved through the same
                ``execute_tool`` path used by the agent runtime, so plugins
                AND legacy gmail/calendar tools are available).
  * ``llm``   — placeholder for now (returns the prompt unchanged so chains
                can be wired and tested before LLM cost is added).
  * ``noop``  — useful for tests and for placeholder steps.

Step shape (validated):

  {
    "id": "step1",                 # required, unique
    "type": "tool",                # tool | llm | noop
    "tool": "core.time_now",       # required for type=tool
    "args": { ... },               # optional dict; supports {{var}} substitution
    "retry": 1,                    # optional, default 0 (extra attempts)
    "on_error": "fail" | "skip"    # optional, default "fail"
  }

Variable substitution
---------------------
Inside any string in ``args``, ``{{ inputs.foo }}`` resolves to
``inputs["foo"]``; ``{{ steps.step1.iso }}`` resolves to the matching
field of step1's output. Missing references resolve to an empty string
(matches ``automation.template`` semantics).

Result shape::

  {
    "status": "ok" | "failed",
    "steps": [ {"id":..., "status":..., "output":..., "error":...}, ... ],
    "outputs": { "step1": {...}, "step2": {...} }
  }

The engine is **synchronous** by design — it's called from request
handlers AND from the Celery task ``run_workflow_task``. Both paths share
the exact same code so behavior is identical.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.agent_executor import ToolExecutionError, execute_tool


logger = logging.getLogger(__name__)


# ---- step / result types ---------------------------------------------------
STEP_TYPES = {"tool", "llm", "noop"}
ON_ERROR_FAIL = "fail"
ON_ERROR_SKIP = "skip"
_VALID_ON_ERROR = {ON_ERROR_FAIL, ON_ERROR_SKIP}

_VAR_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*}}")
_MAX_STEPS = 25


class WorkflowError(RuntimeError):
    pass


@dataclass
class StepResult:
    id: str
    status: str  # ok | failed | skipped
    output: Any = None
    error: Optional[str] = None
    attempts: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
        }


@dataclass
class WorkflowResult:
    status: str  # ok | failed
    steps: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "outputs": {s.id: s.output for s in self.steps if s.status == "ok"},
        }


# ---- helpers ---------------------------------------------------------------
def _resolve_path(scope: dict, dotted: str) -> Any:
    cursor: Any = scope
    for part in dotted.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
            except (ValueError, IndexError):
                return ""
        else:
            return ""
    return cursor


def _substitute(value: Any, scope: dict) -> Any:
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            return str(_resolve_path(scope, match.group(1)))
        return _VAR_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_substitute(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, scope) for k, v in value.items()}
    return value


def _validate_steps(steps: list[dict]) -> None:
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("steps must be a non-empty list")
    if len(steps) > _MAX_STEPS:
        raise WorkflowError(f"too many steps (max {_MAX_STEPS})")
    seen: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowError(f"step #{index} is not an object")
        sid = step.get("id")
        if not isinstance(sid, str) or not sid:
            raise WorkflowError(f"step #{index} is missing an id")
        if sid in seen:
            raise WorkflowError(f"duplicate step id: {sid}")
        seen.add(sid)
        stype = step.get("type", "tool")
        if stype not in STEP_TYPES:
            raise WorkflowError(f"step {sid}: unknown type '{stype}'")
        if stype == "tool" and not step.get("tool"):
            raise WorkflowError(f"step {sid}: type=tool requires a 'tool' name")
        on_err = step.get("on_error", ON_ERROR_FAIL)
        if on_err not in _VALID_ON_ERROR:
            raise WorkflowError(f"step {sid}: invalid on_error '{on_err}'")
        retry = step.get("retry", 0)
        if not isinstance(retry, int) or retry < 0 or retry > 5:
            raise WorkflowError(f"step {sid}: retry must be int 0..5")


# ---- public engine ---------------------------------------------------------
class WorkflowEngine:
    def run(
        self,
        db: Session,
        *,
        user_id: int,
        agent_id: Optional[int],
        steps: list[dict],
        inputs: Optional[dict] = None,
    ) -> WorkflowResult:
        _validate_steps(steps)
        scope: dict = {
            "inputs": dict(inputs or {}),
            "steps": {},
        }
        result = WorkflowResult(status="ok")
        for raw_step in steps:
            sid = raw_step["id"]
            stype = raw_step.get("type", "tool")
            on_error = raw_step.get("on_error", ON_ERROR_FAIL)
            retry = int(raw_step.get("retry", 0))
            args_resolved = _substitute(raw_step.get("args", {}) or {}, scope)

            attempt = 0
            step_output: Any = None
            step_status = "failed"
            step_error: Optional[str] = None
            started = time.monotonic()

            while attempt <= retry:
                attempt += 1
                try:
                    if stype == "tool":
                        step_output = execute_tool(
                            db,
                            raw_step["tool"],
                            dict(args_resolved) if isinstance(args_resolved, dict) else {},
                            user_id,
                            agent_id,
                            retries=0,  # WorkflowEngine owns retry semantics
                        )
                    elif stype == "llm":
                        # Placeholder: echo the prompt. A future change can
                        # plug llm_client here without altering callers.
                        step_output = {"prompt": args_resolved.get("prompt", "")}
                    else:  # noop
                        step_output = {"ok": True}
                    step_status = "ok"
                    step_error = None
                    break
                except ToolExecutionError as exc:
                    step_error = str(exc)
                    logger.warning(
                        "workflow_step_failed",
                        extra={"step": sid, "attempt": attempt, "error": step_error},
                    )
                except Exception as exc:  # noqa: BLE001
                    step_error = str(exc) or exc.__class__.__name__
                    logger.exception(
                        "workflow_step_unexpected_error",
                        extra={"step": sid, "attempt": attempt},
                    )

            duration_ms = int((time.monotonic() - started) * 1000)
            sr = StepResult(
                id=sid,
                status=step_status,
                output=step_output,
                error=step_error,
                attempts=attempt,
                duration_ms=duration_ms,
            )

            if step_status != "ok":
                if on_error == ON_ERROR_SKIP:
                    sr.status = "skipped"
                    result.steps.append(sr)
                    continue
                # ON_ERROR_FAIL → record + abort.
                result.steps.append(sr)
                result.status = "failed"
                return result

            result.steps.append(sr)
            scope["steps"][sid] = step_output

        return result


workflow_engine = WorkflowEngine()


# ---- guard: callers should check the flag ---------------------------------
def is_enabled() -> bool:
    return bool(getattr(settings, "workflow_engine_enabled", False))


__all__ = [
    "WorkflowEngine",
    "WorkflowError",
    "WorkflowResult",
    "StepResult",
    "workflow_engine",
    "is_enabled",
]
