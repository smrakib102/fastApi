cd /opt/ai-agent-system
git pull --ff-only
docker compose -f docker-compose.prod.yml build app worker 2>&1 | tail -10
docker compose -f docker-compose.prod.yml up -d app worker 2>&1 | tail -10
docker compose -f docker-compose.prod.yml exec -T app alembic upgrade head 2>&1 | tail -30
docker compose -f docker-compose.prod.yml exec -T app alembic current 2>&1
docker compose -f docker-compose.prod.yml exec -T postgres psql -U agentuser -d agentdb -c '\dt' | grep -E 'tool_grants|tool_risk_profiles|tool_confirmations|tool_dry_run_log|tool_call_audit'
docker compose -f docker-compose.prod.yml exec -T postgres psql -U agentuser -d agentdb -tAc 'SELECT COUNT(*) FROM tool_risk_profiles'
docker compose -f docker-compose.prod.yml ps
curl -s http://localhost:8000/health | head -c 400
