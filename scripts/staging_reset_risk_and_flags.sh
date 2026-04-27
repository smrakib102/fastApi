#!/bin/bash
set -e
cd /opt/ai-agent-system-staging

POSTGRES_USER=$(grep '^POSTGRES_USER=' .env.staging | cut -d= -f2)
POSTGRES_DB=$(grep '^POSTGRES_DB=' .env.staging | cut -d= -f2)

cat > /tmp/reset_risk.sql <<'SQL'
TRUNCATE tool_risk_profiles;
INSERT INTO tool_risk_profiles (tool_name, risk_tier, requires_hitl, requires_dry_run, description, source) VALUES
('gmail.profile','low',false,false,'Read-only Gmail profile','default'),
('gmail.list_messages','low',false,false,'Read-only Gmail listing','default'),
('gmail.list_drafts','low',false,false,'Read-only Gmail drafts list','default'),
('calendar.list','low',false,false,'Read-only Calendar listing','default'),
('telegram.group_summary','low',false,false,'Read-only Telegram summary','default'),
('gmail.draft','medium',false,false,'Creates a Gmail draft (no send)','default'),
('gmail.send_request','high',true,false,'Creates approval to send Gmail','default'),
('calendar.create_request','high',true,false,'Creates approval to add event','default'),
('gmail.send','critical',true,false,'Sends email (side-effecting)','default'),
('api.request','high',true,true,'External HTTP request (future tool)','default');
SQL

cat /tmp/reset_risk.sql | docker compose -p openclaw-staging -f docker-compose.staging.yml --env-file .env.staging exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

set_env() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" .env.staging; then
    sed -i "s/^${key}=.*/${key}=${value}/" .env.staging
  else
    printf "\n%s=%s\n" "$key" "$value" >> .env.staging
  fi
}

set_env OUTPUT_DEFENCE_MODE enforce
set_env SAFETY_KERNEL_MODE enforce
set_env HITL_ENABLED true
set_env DRY_RUN_ENABLED true
set_env RISK_REGISTRY_ENABLED true

# Restart services

docker compose -p openclaw-staging -f docker-compose.staging.yml --env-file .env.staging up -d --no-deps --force-recreate app worker beat
