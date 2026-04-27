#!/bin/bash
set -e
cd /opt/ai-agent-system-staging

DOCKER="docker compose -p openclaw-staging -f docker-compose.staging.yml --env-file .env.staging"

$DOCKER exec -T app python - <<'PY'
import json
import time
from sqlalchemy import text
from app.db.session import SessionLocal
from app.models.user import User
from app.models.google_account import GoogleAccount
from app.models.tool_confirmation import ToolConfirmation
from app.models.tool_dry_run_log import ToolDryRunLog
from app.models.agent import Agent
from app.services.agent_runtime import _execute_tool_with_audit, resume_hitl_confirmation, execute_agent_run
from app.services.agent_executor import ToolExecutionError
from app.services.agent_builder import _spec_fallback

result = {
    "gmail": {},
    "output_defence": {},
}

with SessionLocal() as db:
    user = (
        db.query(User)
        .join(GoogleAccount, GoogleAccount.user_id == User.id)
        .order_by(User.id.asc())
        .first()
    )
    if not user:
        raise SystemExit("no user with Google account found")

    # Create draft
    draft = _execute_tool_with_audit(
        db,
        tool_name="gmail.draft",
        tool_args={
            "to": "test@example.com",
            "subject": "OpenClaw staging HITL test",
            "body": "This is a staging HITL + dry-run verification message.",
        },
        user_id=user.id,
        agent_id=None,
        run_id=None,
        step_index=None,
        user_text="draft test",
        step_thought="",
        direct_call=True,
    )
    draft_id = (draft or {}).get("id") or (draft or {}).get("draft_id")
    result["gmail"]["draft_id"] = draft_id

    # Enforce HITL + dry-run on gmail.send
    db.execute(
        text(
            """
            UPDATE tool_risk_profiles
               SET requires_hitl=true,
                   requires_dry_run=true,
                   risk_tier='critical',
                   description='Staging HITL+dry-run test',
                   source='test'
             WHERE tool_name = 'gmail.send'
            """
        )
    )
    db.commit()

    try:
        _execute_tool_with_audit(
            db,
            tool_name="gmail.send",
            tool_args={"draft_id": draft_id},
            user_id=user.id,
            agent_id=None,
            run_id=None,
            step_index=None,
            user_text="send test",
            step_thought="",
            direct_call=True,
        )
        result["gmail"]["hitl_blocked"] = False
    except ToolExecutionError as exc:
        result["gmail"]["hitl_blocked"] = "HITL required" in str(exc)

    confirmation = (
        db.query(ToolConfirmation)
        .filter(ToolConfirmation.tool_name == "gmail.send")
        .order_by(ToolConfirmation.id.desc())
        .first()
    )
    result["gmail"]["confirmation_id"] = confirmation.id if confirmation else None

    if confirmation:
        first = resume_hitl_confirmation(db, confirmation_id=confirmation.id, resolved_by="staging-test")
        second = resume_hitl_confirmation(db, confirmation_id=confirmation.id, resolved_by="staging-test")
        result["gmail"]["first_result"] = first
        result["gmail"]["second_result"] = second

        audit_mode = db.execute(
            text(
                """
                SELECT mode FROM tool_call_audit
                 WHERE tool_name = 'gmail.send'
                   AND hitl_resolution = 'approved'
                 ORDER BY id DESC
                 LIMIT 1
                """
            )
        ).first()
        result["gmail"]["audit_mode"] = audit_mode[0] if audit_mode else None

        dry_log = (
            db.query(ToolDryRunLog)
            .filter(ToolDryRunLog.tool_name == "gmail.send")
            .order_by(ToolDryRunLog.id.desc())
            .first()
        )
        result["gmail"]["dry_run_logged"] = bool(dry_log)

    # Output defence enforce: run a tool call and confirm redaction
    agent = _spec_fallback("output defence test")
    agent.tools = ["automation.template"]
    agent.name = "Output Defence Verify " + str(int(time.time()))
    new_agent = Agent(**agent.to_db_payload(user.id))
    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)

    token = "sk-test-1234567890123456"
    tool_payload = json.dumps({"tool": "automation.template", "arguments": {"template": token, "variables": {}}})
    run = execute_agent_run(db, new_agent, user.id, tool_payload, source="staging-verify")
    db.refresh(run)
    result["output_defence"]["run_output"] = run.output_text
    result["output_defence"]["redacted"] = "[redacted]" in (run.output_text or "")

    # Restore gmail.send to baseline
    db.execute(
        text(
            """
            UPDATE tool_risk_profiles
               SET requires_hitl=true,
                   requires_dry_run=false,
                   risk_tier='critical',
                   description='Sends email (side-effecting)',
                   source='default'
             WHERE tool_name = 'gmail.send'
            """
        )
    )
    db.commit()

print(json.dumps(result, indent=2))
PY
