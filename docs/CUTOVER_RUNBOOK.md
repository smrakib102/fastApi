# Unified Chat Cutover Runbook (Phase 9)

End-to-end procedure for promoting the unified chat surface from staging
to production. Every change is reversible by flipping the corresponding
feature flag back to `false` and restarting the API + worker.

## 0. Pre-flight

- [ ] DB snapshot taken (`pg_dump` of the production database).
- [ ] Latest migrations applied: `alembic upgrade head` (idempotent — Phase 1
      memory tables and earlier).
- [ ] Redis reachable from API and worker (used by intent router rate
      limits, OAuth state, and the new Phase 8 OAuth bridge).
- [ ] `PUBLIC_BASE_URL` set to the public HTTPS hostname (required for
      the Telegram → Google OAuth bridge link).
- [ ] All secrets present in env if you intend to enable
      `SECRETS_ENV_ONLY=true`: `OPENAI_API_KEY`, `GEMINI_API_KEY`,
      `TELEGRAM_BOT_TOKEN`.

## 1. Staging rollout

Set in `.env` on staging:

```
UNIFIED_CHAT_ENABLED=true
UNIFIED_CHAT_WEB_ENABLED=true
UNIFIED_CHAT_TELEGRAM_ENABLED=true
UNIFIED_CHAT_NL_AGENT_BUILDER_ENABLED=true
UNIFIED_CHAT_PERMISSION_CARDS_ENABLED=true
PLUGIN_LOADER_ENABLED=true
WORKFLOW_ENGINE_ENABLED=true
```

Restart API + Celery workers. Then run the smoke matrix below.

### Smoke matrix

| # | Channel  | Action                                       | Expected                                                |
|---|----------|----------------------------------------------|---------------------------------------------------------|
| 1 | Web      | `python scripts/smoke_unified_chat.py`       | All checks PASS                                         |
| 2 | Web chat | "create an agent that drafts emails for me" | Agent created; gmail permission card appears           |
| 3 | Web chat | Click "Connect" on permission card           | Browser opens `/google/login`; account links            |
| 4 | Web chat | "add tool core.time_now to <name>"          | "Updated <name>: +tool core.time_now"                   |
| 5 | Web chat | "run <name>"                                 | Run record created; result message rendered             |
| 6 | API      | `POST /workflows/run` 2-step chain           | `status:"ok"` with both step outputs                    |
| 7 | Telegram | `/start` then "create an agent ..."         | Same flow; permission cards as inline keyboard          |
| 8 | Telegram | Tap "Connect" on permission card             | Bot DMs `/google/oauth/bridge/<token>` link             |
| 9 | Telegram | Open bridge link in browser                  | Google consent → callback → `GoogleAccount` row created |

If anything fails, see **Rollback** below.

## 2. Production rollout

Apply the same env block to prod and restart. Run the smoke script
against prod with a real session cookie:

```
$env:BASE_URL = "https://app.example.com"
$env:SESSION_COOKIE = "session=<copied-from-browser>"
python scripts/smoke_unified_chat.py
```

Watch logs for 5 minutes:
- `chat_intent_routed` — should be flowing.
- `permission_request_created` — expected when users build agents.
- `workflow_step_*` — only when `/workflows/*` is exercised.
- No new `ERROR` entries from `app.services.chat_service`,
  `app.services.workflow_engine`, or `app.api.routes.telegram`.

## 3. Deprecate the legacy wizard

The legacy wizard at `/dashboard/agents/new` now renders a deprecation
banner pointing to `/dashboard/chat` whenever `UNIFIED_CHAT_ENABLED=true`.
After one week of stable operation:

1. Remove the link to the wizard from the agents list page.
2. Replace the wizard route body with a 302 to `/dashboard/chat`.
3. Delete `app/templates/agents_new.html` once analytics confirm zero
   traffic for 7 consecutive days.

Do **not** delete the route in the same release that flips the flag.

## 4. Rollback

Any phase can be disabled independently — flags are additive.

| Symptom                                          | Flip to false                                  |
|--------------------------------------------------|------------------------------------------------|
| Web chat broken                                  | `UNIFIED_CHAT_WEB_ENABLED`                     |
| Telegram broken                                  | `UNIFIED_CHAT_TELEGRAM_ENABLED`                |
| Agent creation produces wrong specs              | `UNIFIED_CHAT_NL_AGENT_BUILDER_ENABLED`        |
| Permission cards confuse users                   | `UNIFIED_CHAT_PERMISSION_CARDS_ENABLED`        |
| Workflow runs misbehave                          | `WORKFLOW_ENGINE_ENABLED`                      |
| Plugin tool returns bad output                   | `PLUGIN_LOADER_ENABLED` (falls back to legacy) |
| Telegram OAuth bridge URL wrong/unreachable      | unset `PUBLIC_BASE_URL`                        |
| Master kill-switch                               | `UNIFIED_CHAT_ENABLED=false`                   |

After flipping a flag, restart API + workers. No DB rollback is needed —
all phases are read-mostly or additive (Phase 1 added two tables that
remain harmless when unused).

## 5. Post-cutover cleanup checklist

- [ ] Remove `agents_new.html` link from agents list (after grace period).
- [ ] Move `SECRETS_ENV_ONLY=true` to prod once admins confirm they no
      longer edit secrets through the admin UI.
- [ ] Document new chat-only onboarding in user-facing docs.
