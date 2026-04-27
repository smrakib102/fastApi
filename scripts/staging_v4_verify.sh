#!/bin/bash
set -e
cd /opt/ai-agent-system-staging

set_env() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" .env.staging; then
    sed -i "s/^${key}=.*/${key}=${value}/" .env.staging
  else
    printf "\n%s=%s\n" "$key" "$value" >> .env.staging
  fi
}

set_env SAFETY_KERNEL_MODE enforce
set_env HITL_ENABLED true
set_env DRY_RUN_ENABLED true
set_env RISK_REGISTRY_ENABLED true
set_env OUTPUT_DEFENCE_MODE shadow

DOCKER="docker compose -p openclaw-staging -f docker-compose.staging.yml --env-file .env.staging"
$DOCKER up -d --no-deps --force-recreate app worker beat >/tmp/staging_restart.log 2>&1 || true

$DOCKER exec -T app python - <<'PY'
import json
import time
from datetime import datetime, timezone

from sqlalchemy import text
from app.db.session import SessionLocal
from app.models.user import User
from app.models.tool_confirmation import ToolConfirmation
from app.models.tool_dry_run_log import ToolDryRunLog
from app.models.summary_schedule import SummarySchedule
from app.models.telegram_message import TelegramMessage
from app.services.agent_runtime import _execute_tool_with_audit, resume_hitl_confirmation, execute_agent_run
from app.services.agent_executor import execute_tool_local, ToolExecutionError
from app.services.agent_builder import agent_builder, _spec_fallback
from app.api.routes.telegram import _bind_group_to_agent
from app.worker.tasks import send_summaries

result = {
    "hitl": {},
    "dry_run": {},
    "plugin_schema": {},
    "output_defence": {},
    "telegram_flow": {},
}

with SessionLocal() as db:
    user = db.query(User).order_by(User.id.asc()).first()
    if not user:
        raise SystemExit("no user found")

    # --- HITL execution + idempotency (automation.template) ---
    db.execute(
        text(
            """
            INSERT INTO tool_risk_profiles (tool_name, risk_tier, requires_hitl, requires_dry_run, description, source)
            VALUES (:tool, 'high', true, false, 'HITL test', 'test')
            ON CONFLICT (tool_name) DO UPDATE SET
              requires_hitl=true,
              requires_dry_run=false,
              risk_tier='high',
              description='HITL test',
              source='test'
            """
        ),
        {"tool": "automation.template"},
    )
    db.commit()

    try:
        _execute_tool_with_audit(
            db,
            tool_name="automation.template",
            tool_args={"template": "HITL {{x}}", "variables": {"x": "ok"}},
            user_id=user.id,
            agent_id=None,
            run_id=None,
            step_index=None,
            user_text="hitl test",
            step_thought="",
            direct_call=True,
        )
        result["hitl"]["blocked"] = False
    except ToolExecutionError as exc:
        result["hitl"]["blocked"] = "HITL required" in str(exc)

    confirmation = (
        db.query(ToolConfirmation)
        .filter(ToolConfirmation.tool_name == "automation.template")
        .order_by(ToolConfirmation.id.desc())
        .first()
    )
    result["hitl"]["confirmation_id"] = confirmation.id if confirmation else None

    if confirmation:
        first = resume_hitl_confirmation(db, confirmation_id=confirmation.id, resolved_by="staging-test")
        second = resume_hitl_confirmation(db, confirmation_id=confirmation.id, resolved_by="staging-test")
        result["hitl"]["first_result"] = first
        result["hitl"]["second_result"] = second

        audit_rows = db.execute(
            text(
                """
                SELECT id FROM tool_call_audit
                 WHERE tool_name = :tool
                   AND hitl_resolution = 'approved'
                 ORDER BY id DESC
                 LIMIT 5
                """
            ),
            {"tool": "automation.template"},
        ).all()
        result["hitl"]["audit_approved_count"] = len(audit_rows)

    # Reset automation.template to non-HITL for subsequent tests
    db.execute(
        text(
            """
            UPDATE tool_risk_profiles
               SET requires_hitl=false,
                   requires_dry_run=false,
                   risk_tier='low',
                   description='reset after HITL test',
                   source='test'
             WHERE tool_name = :tool
            """
        ),
        {"tool": "automation.template"},
    )
    db.commit()

    # --- Dry-run enforcement (automation.json_path) ---
    db.execute(
        text(
            """
            INSERT INTO tool_risk_profiles (tool_name, risk_tier, requires_hitl, requires_dry_run, description, source)
            VALUES (:tool, 'high', false, true, 'Dry-run test', 'test')
            ON CONFLICT (tool_name) DO UPDATE SET
              requires_hitl=false,
              requires_dry_run=true,
              risk_tier='high',
              description='Dry-run test',
              source='test'
            """
        ),
        {"tool": "automation.json_path"},
    )
    db.commit()

    dry_result = _execute_tool_with_audit(
        db,
        tool_name="automation.json_path",
        tool_args={"data": {"a": {"b": "c"}}, "path": "a.b"},
        user_id=user.id,
        agent_id=None,
        run_id=None,
        step_index=None,
        user_text="dry run",
        step_thought="",
        direct_call=True,
    )
    result["dry_run"]["result"] = dry_result

    dry_log = (
        db.query(ToolDryRunLog)
        .filter(ToolDryRunLog.tool_name == "automation.json_path")
        .order_by(ToolDryRunLog.id.desc())
        .first()
    )
    result["dry_run"]["logged"] = bool(dry_log)

    dry_audit = db.execute(
        text(
            """
            SELECT mode FROM tool_call_audit
             WHERE tool_name = :tool
             ORDER BY id DESC
             LIMIT 1
            """
        ),
        {"tool": "automation.json_path"},
    ).first()
    result["dry_run"]["audit_mode"] = dry_audit[0] if dry_audit else None

    # --- Plugin schema validation safety ---
    try:
        plugin_out = execute_tool_local(
            db,
            "automation.template",
            {"template": "Hi {{name}}", "variables": {"name": "world"}},
            user.id,
            None,
        )
        result["plugin_schema"]["ok"] = True
        result["plugin_schema"]["output"] = plugin_out
    except ToolExecutionError as exc:
        result["plugin_schema"]["ok"] = False
        result["plugin_schema"]["error"] = str(exc)

    # --- Output defence behavior ---
    from app.models.agent import Agent
    # Guard against null counters in user_performance
    db.execute(
        text(
            """
            UPDATE user_performance
               SET success_count = COALESCE(success_count, 0),
                   failure_count = COALESCE(failure_count, 0),
                   success_rate = COALESCE(success_rate, 0)
             WHERE user_id = :user_id
            """
        ),
        {"user_id": user.id},
    )
    db.commit()

    agent = _spec_fallback("output defence test")
    agent.tools = ["automation.template"]
    agent.name = f"Output Defence Test Agent {int(time.time())}"
    new_agent = Agent(**agent.to_db_payload(user.id))
    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)

    tool_payload = json.dumps({"tool": "automation.template", "arguments": {"template": "sk-test-1234567890123456", "variables": {}}})
    run = execute_agent_run(db, new_agent, user.id, tool_payload, source="staging-verify")
    db.refresh(run)
    result["output_defence"]["run_output"] = run.output_text
    result["output_defence"]["redacted"] = "[redacted]" in (run.output_text or "")

    # --- Telegram end-to-end flow (structural) ---
    build = agent_builder.from_natural_language(db, user=user, text="Create an agent that monitors my Telegram group and sends daily summary at 6pm")
    result["telegram_flow"]["builder_status"] = build.status
    result["telegram_flow"]["agent_id"] = build.agent_id

    if build.agent_id:
        agent_row = db.get(Agent, build.agent_id)
        _bind_group_to_agent(db, user_id=user.id, agent=agent_row, group_chat_id="-1001234567890")
        db.commit()

        msg = TelegramMessage(
            user_id=user.id,
            chat_id="-1001234567890",
            chat_type="group",
            message_id="test-1",
            sender_id="tester",
            sender_name="Tester",
            text="Hello team",
            sent_at=datetime.now(timezone.utc),
            raw_json=json.dumps({"text": "Hello team"}),
        )
        db.add(msg)
        db.commit()

        schedule = (
            db.query(SummarySchedule)
            .filter(SummarySchedule.user_id == user.id, SummarySchedule.chat_id == "-1001234567890")
            .one_or_none()
        )
        result["telegram_flow"]["schedule_created"] = bool(schedule)

        try:
            send_summaries()
            db.refresh(schedule)
            result["telegram_flow"]["last_sent_at"] = schedule.last_sent_at.isoformat() if schedule and schedule.last_sent_at else None
            result["telegram_flow"]["send_summaries_ok"] = True
        except Exception as exc:
            result["telegram_flow"]["send_summaries_ok"] = False
            result["telegram_flow"]["send_summaries_error"] = str(exc)

print(json.dumps(result, indent=2))
PY
