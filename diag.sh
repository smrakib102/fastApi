#!/bin/bash
cd /opt/ai-agent-system
source .env
echo "=== alembic current ==="
docker compose -f docker-compose.prod.yml exec -T app alembic current
echo "=== matching tables ==="
docker compose -f docker-compose.prod.yml exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('tool_call_audit','tool_credentials','blocked_domains','feature_flags') ORDER BY tablename"
echo "=== tool_credentials row count if exists ==="
docker compose -f docker-compose.prod.yml exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT COUNT(*) FROM tool_credentials" 2>/dev/null || echo "does not exist"
echo "=== latest 5 commits ==="
git log --oneline -n 5
