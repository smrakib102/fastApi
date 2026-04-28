# Telegram Tenant Bots Spec

Goal
- Support per-tenant Telegram bots with one-time connect (no expiry), safe disconnect, and re-connect.
- Keep a single webhook endpoint while allowing many tenant-specific bot tokens.
- Preserve auditability, isolation, and low operational risk.

Non-Goals
- Auto-creating bots via BotFather (not supported by Telegram).
- Multiple webhooks per bot (Telegram only allows one).

Definitions
- Tenant: a company/org account in the app.
- Bot token: the Telegram bot token created manually in BotFather.
- Link: association between a Telegram user (telegram_user_id) and a tenant.

Data Model (conceptual)
- tenant_telegram_bot
  - tenant_id (PK)
  - bot_token_encrypted
  - bot_username
  - bot_id
  - webhook_url
  - status (active | disconnected | revoked)
  - connected_at
  - disconnected_at
  - connected_by_user_id

- tenant_telegram_link
  - tenant_id
  - telegram_user_id
  - display_name
  - status (active | disabled)
  - linked_at
  - linked_by_user_id

- audit_log
  - action (telegram_bot_connected | telegram_bot_disconnected | telegram_linked | telegram_unlinked)
  - actor_user_id
  - tenant_id
  - metadata

Security and Secrets
- Store bot tokens encrypted at rest.
- Never echo tokens in UI responses; show masked values only.
- If SECRETS_ENV_ONLY is enabled, the per-tenant flow should be disabled or routed to an external secrets store.
- Validate webhook secret on inbound updates.

User Experience Flow
1) Connect modal
   - Field: Bot token (required).
   - Help text: Step-by-step BotFather instructions.
   - Button: Connect.

2) Connect action (server)
   - Validate token using getMe.
   - Store bot identity (bot_username, bot_id).
   - Call setWebhook to the shared endpoint with a webhook secret.
   - Persist the bot token encrypted to tenant_telegram_bot.

3) Post-connect UI
   - Show bot username, webhook URL, and status.
   - Provide Open Telegram button:
     https://t.me/<bot_username>?start=<tenant_id_or_uuid>

4) One-time link via /start
   - When /start payload is received, link telegram_user_id to tenant.
   - If already linked, update display_name and keep status active.

5) Normal usage
   - Every Telegram update is routed to the tenant by bot token.
   - All actions check tenant_telegram_link status before processing.

Disconnect and Re-connect
- Disconnect button with confirmation modal.
- Disconnect action (server):
  - Call deleteWebhook for the bot token.
  - Mark tenant_telegram_bot status = disconnected.
  - Clear bot token or mark revoked (do not reuse).
  - Set all tenant_telegram_link status = disabled.
  - Write audit log event.

- Re-connect action:
  - Same as Connect action; creates a new active bot record.
  - Links are re-enabled only after /start for each user.

Webhook Routing and Validation
- One endpoint receives updates for all tenant bots.
- Determine tenant by matching bot token to tenant_telegram_bot record.
- Reject updates if:
  - No active bot record for that token.
  - Webhook secret mismatch.

Admin and Observability
- Admin status endpoint per tenant:
  - bot_username, bot_id, webhook_url, pending_updates, last_error_message.
- Startup log:
  - Log bot_username + webhook URL for each active tenant bot.

Operational Runbook
- Token rotation:
  - Disconnect old bot.
  - Connect new token.
  - Verify webhook and /start flow.
- Incident response:
  - Check admin status endpoint and logs for webhook errors.
  - Verify tenant bot matches the Telegram bot being used.

UI Copy (BotFather steps)
- Open Telegram and search for BotFather.
- Send /newbot and follow prompts.
- Copy the bot token from BotFather.
- Paste the token here and click Connect.

Acceptance Criteria
- Multiple tenants can connect different bots without conflict.
- A tenant can disconnect and switch to a new bot safely.
- Telegram actions fail closed when not linked or bot is disconnected.
- Admin can verify bot identity + webhook status at any time.
