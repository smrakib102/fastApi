# Phase 2 Shadow OAuth Operations Runbook

This document covers operational safety procedures for the Phase 2 Shadow OAuth system. It does not introduce new application logic.

## Signature Secret Rotation Runbook

### Pre-rotation state
- Primary signature secret is active and used for signing callbacks.
- Secondary signature secret is only used for verification fallback.
- Confirm logs show normal signature verification with minimal or zero fallback usage.

### Rotation steps
1. Generate a new secondary secret and store it securely.
2. Deploy configuration with both secrets active:
   - `NEXTAUTH_SIGNATURE_SECRET` remains the current primary.
   - `NEXTAUTH_SIGNATURE_SECONDARY_SECRET` is the newly generated secret.
3. Wait through a full traffic stabilization window (at least one metrics window + cache TTL), monitoring for signature errors.
4. Promote the secondary to primary:
   - Set `NEXTAUTH_SIGNATURE_SECRET` to the previous secondary.
   - Generate a new secondary and set `NEXTAUTH_SIGNATURE_SECONDARY_SECRET` to the new value.
5. Validate that fallback usage rate returns to baseline and no signature failures occur.

### Safety rules
- Never disable both secrets at the same time.
- Never rotate during an active incident or while kill switch is engaged.
- Logs must confirm fallback usage rate stays low and stable during rotation.

## Migration Run Procedure (Alembic)

### Apply migration
1. Confirm the current Alembic head is `0027_oauth_credentials_vault`.
2. Apply the new migration:
   - `alembic upgrade 0028_oauth_credentials_invalid_state`
3. Validate the new columns on `oauth_credentials`:
   - `invalid_state` (boolean, default false)
   - `invalid_reason` (text, nullable)
   - `invalid_at` (timestamp, nullable)

### Rollback strategy
1. Roll back only if the application is halted or NextAuth is disabled.
2. Use the downgrade:
   - `alembic downgrade 0027_oauth_credentials_vault`
3. Verify the schema no longer contains invalid-state columns.

### Post-migration checks
- Confirm existing unique constraint remains on `(user_id, provider, provider_account_id)`.
- Confirm no runtime dependency changes were introduced.
- Verify `/metrics/oauth` loads and the application health endpoint is green.

## Incident Response Playbook

### A) Signature failure spike
- Immediately set `ENABLE_NEXTAUTH_OAUTH=false`.
- Keep legacy OAuth path active.
- Investigate signature headers, time drift, and secret alignment.

### B) Vault write failure spike
- Set `ENABLE_VAULT_SYSTEM=false` to pause shadow writes.
- Keep legacy OAuth active.
- Investigate DB availability, locks, and write latency.

### C) Invalid credential explosion
- Keep legacy OAuth active and NextAuth optional.
- Trigger bulk re-auth via PermissionService (batch per tool/user segment).
- Keep `invalid_state` markings for audit and follow-up.

## Monitoring Checklist (Ops Ready)

Required dashboards and alerts:
- OAuth callback success rate
- Callback latency (avg and p95)
- Vault write success rate
- Invalid credential count (`invalid_state`)
- Timestamp drift anomaly rate
- Signature fallback usage rate

## Safe State Guarantee

- System always retains legacy OAuth fallback.
- Vault data is write-only; no runtime path depends on it.
- Vault corruption does not affect agent execution.
- Rollback does not require a database restore.
