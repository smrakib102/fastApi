"""PermissionService — Phase 4 of the unified chat refactor.

Owns the lifecycle of in-chat permission requests:

  1. AgentBuilder (or any other component) detects a missing tool/credential.
  2. PermissionService.request(...) creates / reuses a ToolRequest row and
     returns a structured `permission_request` chat-object.
  3. ChatService attaches the chat-object to its ChatResponse.actions list.
  4. Channel adapters render it natively:
       - Web  → action card with Allow / Connect / Deny buttons.
       - Telegram → inline keyboard.
  5. User decides; the resolver endpoint (web) or callback handler
     (telegram) calls PermissionService.resolve(...) which updates the
     ToolRequest, optionally provisions OAuth links, and returns a final
     human-readable message.

Reuses the existing `ToolRequest` and `ToolRegistry` tables — no schema
changes. OAuth provisioning is delegated to existing `google_oauth`
routes; this service only emits the link.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tool_credential import ToolCredential
from app.models.tool_registry import ToolRegistry
from app.models.tool_request import ToolRequest
from app.models.user import User


logger = logging.getLogger(__name__)


# ---- chat-object schema constants -----------------------------------------
ACTION_PERMISSION_REQUEST = "permission_request"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_CONNECT = "connect"  # for OAuth tools
DECISION_SKIP = "skip"

VALID_DECISIONS = {DECISION_ALLOW, DECISION_DENY, DECISION_CONNECT, DECISION_SKIP}


# Tools that require OAuth instead of API keys. Pulled from ToolRegistry.auth_type
# when available; this set is a safe fallback.
_OAUTH_AUTH_TYPES = {"oauth", "oauth2", "google_oauth"}


@dataclass
class PermissionAction:
    """One button on a permission-request card."""

    label: str
    decision: str
    url: Optional[str] = None  # OAuth deep link, when applicable


@dataclass
class PermissionRequest:
    """Channel-agnostic chat object."""

    type: str
    request_id: int
    tool_name: str
    reason: str
    actions: list[PermissionAction] = field(default_factory=list)
    auth_type: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "reason": self.reason,
            "auth_type": self.auth_type,
            "actions": [
                {"label": a.label, "decision": a.decision, "url": a.url}
                for a in self.actions
            ],
        }


# ---- service ---------------------------------------------------------------
class PermissionService:
    """Stateless. Caller commits the transaction."""

    # ---- internal helpers ----
    def _find_tool(self, db: Session, tool_name: str) -> Optional[ToolRegistry]:
        stmt = select(ToolRegistry).where(ToolRegistry.name == tool_name)
        return db.execute(stmt).scalars().first()

    def _user_has_credential(self, db: Session, user_id: int, tool_id: int) -> bool:
        stmt = select(ToolCredential).where(
            ToolCredential.user_id == user_id,
            ToolCredential.tool_id == tool_id,
        )
        return db.execute(stmt).scalars().first() is not None

    def _existing_pending(
        self, db: Session, user_id: int, tool_name: str
    ) -> Optional[ToolRequest]:
        stmt = (
            select(ToolRequest)
            .where(ToolRequest.user_id == user_id)
            .where(ToolRequest.tool_name == tool_name)
            .where(ToolRequest.status.in_(["pending", "waiting_oauth"]))
            .order_by(ToolRequest.created_at.desc())
        )
        return db.execute(stmt).scalars().first()

    def _build_actions(
        self, *, tool: Optional[ToolRegistry], request_id: int
    ) -> tuple[list[PermissionAction], Optional[str]]:
        auth_type = (tool.auth_type or "").lower() if tool else ""
        actions: list[PermissionAction]
        if auth_type in _OAUTH_AUTH_TYPES:
            # OAuth path: surface a connect URL the channel adapter can attach.
            connect_url = self._oauth_url_for(tool)
            actions = [
                PermissionAction(label="Connect", decision=DECISION_CONNECT, url=connect_url),
                PermissionAction(label="Deny", decision=DECISION_DENY),
            ]
        else:
            # API-key path: user explicitly grants; key entry happens in the
            # next message (Telegram) or in the settings dialog (web).
            actions = [
                PermissionAction(label="Allow", decision=DECISION_ALLOW),
                PermissionAction(label="Skip", decision=DECISION_SKIP),
                PermissionAction(label="Deny", decision=DECISION_DENY),
            ]
        return actions, auth_type or None

    def _oauth_url_for(self, tool: Optional[ToolRegistry]) -> Optional[str]:
        if not tool:
            return None
        provider = (tool.provider or "").lower()
        # Only Google OAuth wired today; extend as more providers come online.
        if provider in {"google", "gmail", "calendar"}:
            return "/google/login"
        return None

    # ---- public API ----
    def request(
        self,
        db: Session,
        *,
        user: User,
        tool_name: str,
        reason: str,
    ) -> PermissionRequest:
        """Create or reuse a ToolRequest and return a chat-object."""
        tool = self._find_tool(db, tool_name)

        # Short-circuit: if tool exists and credential exists, no request needed.
        if tool and self._user_has_credential(db, user.id, tool.id):
            return PermissionRequest(
                type=ACTION_PERMISSION_REQUEST,
                request_id=0,
                tool_name=tool_name,
                reason="(already authorized)",
                actions=[],
                auth_type=(tool.auth_type or None),
            )

        existing = self._existing_pending(db, user.id, tool_name)
        if existing:
            req = existing
        else:
            req = ToolRequest(
                user_id=user.id,
                tool_name=tool_name,
                details=reason,
                status="pending",
            )
            db.add(req)
            db.flush()

        actions, auth_type = self._build_actions(tool=tool, request_id=req.id)
        return PermissionRequest(
            type=ACTION_PERMISSION_REQUEST,
            request_id=req.id,
            tool_name=tool_name,
            reason=reason,
            actions=actions,
            auth_type=auth_type,
        )

    def request_many(
        self,
        db: Session,
        *,
        user: User,
        tool_names: list[str],
        reason: str,
    ) -> list[PermissionRequest]:
        out: list[PermissionRequest] = []
        for name in tool_names:
            try:
                pr = self.request(db, user=user, tool_name=name, reason=reason)
                # Skip "already authorized" entries from the chat surface.
                if pr.request_id == 0:
                    continue
                out.append(pr)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "permission_request_failed",
                    extra={"user_id": user.id, "tool_name": name},
                )
        return out

    def resolve(
        self,
        db: Session,
        *,
        user: User,
        request_id: int,
        decision: str,
    ) -> dict:
        """Apply a user decision. Returns {ok, message, status, oauth_url?}."""
        if decision not in VALID_DECISIONS:
            return {"ok": False, "message": "Unknown decision."}

        req = db.get(ToolRequest, request_id)
        if not req or req.user_id != user.id:
            return {"ok": False, "message": "Permission request not found."}

        tool = self._find_tool(db, req.tool_name)

        if decision == DECISION_DENY:
            req.status = "denied"
            db.flush()
            # S5: audit denial so admins can see permission patterns.
            try:
                from app.services.audit_log import record_audit

                record_audit(
                    db,
                    user_id=user.id,
                    action="permission_denied",
                    resource_type="tool_request",
                    resource_id=str(req.id),
                    metadata={"tool_name": req.tool_name},
                )
            except Exception:
                logger.debug("permission_audit_failed", extra={"request_id": req.id})
            return {
                "ok": True,
                "status": req.status,
                "message": f"Denied access to {req.tool_name}. The agent will skip it.",
            }

        if decision == DECISION_SKIP:
            req.status = "skipped"
            db.flush()
            try:
                from app.services.audit_log import record_audit

                record_audit(
                    db,
                    user_id=user.id,
                    action="permission_skipped",
                    resource_type="tool_request",
                    resource_id=str(req.id),
                    metadata={"tool_name": req.tool_name},
                )
            except Exception:
                logger.debug("permission_audit_failed", extra={"request_id": req.id})
            return {
                "ok": True,
                "status": req.status,
                "message": f"Skipped {req.tool_name}. You can grant access later.",
            }

        if decision == DECISION_CONNECT:
            req.status = "waiting_oauth"
            db.flush()
            url = self._oauth_url_for(tool)
            if not url:
                return {
                    "ok": False,
                    "status": req.status,
                    "message": (
                        f"OAuth connection isn't configured for {req.tool_name} yet. "
                        "Try the API-key path instead."
                    ),
                }
            return {
                "ok": True,
                "status": req.status,
                "oauth_url": url,
                "message": f"Open the link to connect {req.tool_name}: {url}",
            }

        # DECISION_ALLOW — user agreed to provide an API key. Adapters
        # take over from here (web opens a key dialog; Telegram waits
        # for the next message containing the key).
        req.status = "awaiting_key"
        db.flush()
        return {
            "ok": True,
            "status": req.status,
            "message": (
                f"Great — paste the API key for {req.tool_name} as your next message."
                if tool is None or (tool.auth_type or "").lower() == "api_key"
                else f"Allowed {req.tool_name}. Configure it in Settings to finish."
            ),
        }


permission_service = PermissionService()


__all__ = [
    "PermissionService",
    "PermissionRequest",
    "PermissionAction",
    "permission_service",
    "ACTION_PERMISSION_REQUEST",
    "DECISION_ALLOW",
    "DECISION_DENY",
    "DECISION_CONNECT",
    "DECISION_SKIP",
]
