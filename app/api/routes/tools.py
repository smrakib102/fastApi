import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from email.message import EmailMessage

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.routes.google_oauth import _ensure_token, _get_default_account
from app.core.config import settings
from app.models.approval import Approval
from app.services.usage_limits import check_and_record_usage
from app.services.audit_log import record_audit

router = APIRouter()

logger = logging.getLogger(__name__)

_nonce_cache: dict[str, float] = {}

GMAIL_DRAFT_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class ToolExecuteRequest(BaseModel):
    name: str
    arguments: dict = {}


def _require_tool_token(x_tool_token: str | None) -> None:
    if not settings.tool_api_token:
        raise HTTPException(status_code=403, detail="Tool API disabled")
    if x_tool_token != settings.tool_api_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _canonical_payload(payload: ToolExecuteRequest) -> str:
    return json.dumps(payload.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_internal_signature(
    user_id: int,
    tool_payload: ToolExecuteRequest,
    timestamp: int,
    nonce: str,
    signature: str | None,
) -> None:
    if not signature:
        raise HTTPException(status_code=401, detail="Missing internal signature")

    now = int(time.time())
    if abs(now - timestamp) > 30:
        raise HTTPException(status_code=401, detail="Request expired")

    payload_text = _canonical_payload(tool_payload)
    message = f"{user_id}:{timestamp}:{nonce}:{payload_text}".encode("utf-8")
    expected = hmac.new(
        settings.tool_api_token.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid internal signature")


def _validate_nonce(nonce: str) -> None:
    try:
        uuid.UUID(nonce)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid nonce") from exc

    now = time.time()
    cutoff = now - 60
    expired = [key for key, expires_at in _nonce_cache.items() if expires_at <= cutoff]
    for key in expired:
        _nonce_cache.pop(key, None)

    if nonce in _nonce_cache:
        raise HTTPException(status_code=401, detail="Nonce replay detected")

    _nonce_cache[nonce] = now + 60


def _execute_tool_internal(
    payload: ToolExecuteRequest,
    db: Session,
    internal_user_id: int,
    internal_agent_id: int | None,
) -> dict:
    if "user_id" in payload.arguments:
        raise HTTPException(status_code=400, detail="user_id must not be provided")

    usage_provider = payload.arguments.get("provider")
    usage_tokens = payload.arguments.get("tokens")
    if usage_provider and usage_tokens:
        check_and_record_usage(
            db,
            int(internal_user_id),
            str(usage_provider),
            int(usage_tokens),
        )

    record_audit(
        db,
        int(internal_user_id),
        "tool_execute",
        "tool",
        payload.name,
        {"agent_id": internal_agent_id},
    )

    if payload.name == "gmail.draft":
        return _gmail_draft(payload.arguments, db, internal_user_id)
    if payload.name == "gmail.send_request":
        return _gmail_send_request(payload.arguments, db, internal_user_id, internal_agent_id)
    if payload.name == "gmail.send":
        return _gmail_send(payload.arguments, db, internal_user_id)
    if payload.name == "gmail.profile":
        return _gmail_profile(db, internal_user_id)
    if payload.name == "calendar.list":
        return _calendar_list(db, internal_user_id)
    if payload.name == "calendar.create_request":
        return _calendar_create_request(payload.arguments, db, internal_user_id, internal_agent_id)
    if payload.name == "gmail.list_messages":
        return _gmail_list_messages(payload.arguments, db, internal_user_id)
    if payload.name == "gmail.list_drafts":
        return _gmail_list_drafts(payload.arguments, db, internal_user_id)

    raise HTTPException(status_code=404, detail="Unknown tool")


def _build_raw_message(to: str, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw_bytes = message.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


def _gmail_draft(args: dict, db: Session, user_id: int) -> dict:
    to = args.get("to")
    subject = args.get("subject")
    body = args.get("body")
    thread_id = args.get("thread_id")

    if not to or not subject or not body:
        raise HTTPException(status_code=400, detail="Missing to/subject/body")

    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}

    payload: dict = {"message": {"raw": _build_raw_message(to, subject, body)}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    response = httpx.post(GMAIL_DRAFT_URL, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to create draft")

    return response.json()


def _gmail_send_request(args: dict, db: Session, user_id: int, agent_id: int | None) -> dict:
    draft_id = args.get("draft_id")
    reason = args.get("reason")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    approval = Approval(
        user_id=user_id,
        agent_id=agent_id,
        type="gmail.send",
        payload=json.dumps({"draft_id": draft_id, "reason": reason}),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {"approval_id": approval.id, "status": approval.status}


def _gmail_send(args: dict, db: Session, user_id: int) -> dict:
    draft_id = args.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.post(GMAIL_SEND_URL, headers=headers, json={"id": draft_id}, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to send draft")

    return response.json()


def _gmail_profile(db: Session, user_id: int) -> dict:
    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(GMAIL_PROFILE_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Gmail profile")
    return response.json()


def _calendar_list(db: Session, user_id: int) -> dict:
    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(CALENDAR_LIST_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch calendar list")
    return response.json()


def _gmail_list_messages(args: dict, db: Session, user_id: int) -> dict:
    max_results = args.get("max_results", 10)
    query = args.get("q")
    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    params: dict = {"maxResults": max_results}
    if query:
        params["q"] = query
    response = httpx.get(GMAIL_MESSAGES_URL, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to list inbox")

    payload = response.json()
    messages = payload.get("messages", []) or []
    detailed_messages: list[dict] = []

    for message in messages:
        msg_id = message.get("id")
        if not msg_id:
            continue

        detail_params = {
            "format": "metadata",
            "metadataHeaders": ["Subject", "From", "Date"],
        }
        detail_url = f"{GMAIL_MESSAGES_URL}/{msg_id}"
        detail_resp = httpx.get(detail_url, headers=headers, params=detail_params, timeout=30)
        if detail_resp.status_code != 200:
            detailed_messages.append({
                "id": msg_id,
                "threadId": message.get("threadId"),
                "error": "Failed to load message metadata",
            })
            continue

        detail = detail_resp.json()
        headers_list = detail.get("payload", {}).get("headers", []) or []
        header_map = {item.get("name"): item.get("value") for item in headers_list}
        detailed_messages.append({
            "id": msg_id,
            "threadId": message.get("threadId"),
            "subject": header_map.get("Subject"),
            "from": header_map.get("From"),
            "date": header_map.get("Date"),
            "snippet": detail.get("snippet"),
        })

    return {
        "messages": detailed_messages,
        "nextPageToken": payload.get("nextPageToken"),
        "resultSizeEstimate": payload.get("resultSizeEstimate"),
    }


def _gmail_list_drafts(args: dict, db: Session, user_id: int) -> dict:
    max_results = args.get("max_results", 10)
    account = _ensure_token(db, _get_default_account(db, user_id))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    params = {"maxResults": max_results}
    response = httpx.get(GMAIL_DRAFT_URL, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to list drafts")
    return response.json()


def _calendar_create_request(args: dict, db: Session, user_id: int, agent_id: int | None) -> dict:
    calendar_id = args.get("calendar_id")
    summary = args.get("summary")
    start = args.get("start")
    end = args.get("end")
    if not calendar_id or not summary or not start or not end:
        raise HTTPException(status_code=400, detail="Missing calendar fields")

    approval = Approval(
        user_id=user_id,
        agent_id=agent_id,
        type="calendar.create",
        payload=json.dumps(
            {
                "calendar_id": calendar_id,
                "summary": summary,
                "description": args.get("description"),
                "start": start,
                "end": end,
                "attendees": args.get("attendees"),
            }
        ),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {"approval_id": approval.id, "status": approval.status}


@router.get("/manifest")
def tool_manifest(x_tool_token: str | None = Header(default=None)):
    _require_tool_token(x_tool_token)

    return {
        "tools": [
            {
                "name": "gmail.draft",
                "description": "Create a Gmail draft.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "thread_id": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "gmail.send_request",
                "description": "Create an approval request to send a Gmail draft.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["draft_id"],
                },
            },
            {
                "name": "gmail.send",
                "description": "Send a Gmail draft (approval should be enforced upstream).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string"},
                    },
                    "required": ["draft_id"],
                },
            },
            {
                "name": "gmail.profile",
                "description": "Fetch Gmail profile data.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "calendar.list",
                "description": "List Google calendars.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "calendar.create_request",
                "description": "Create an approval request for a calendar event.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "description": {"type": "string"},
                        "start": {"type": "object"},
                        "end": {"type": "object"},
                        "attendees": {"type": "array"},
                    },
                    "required": ["calendar_id", "summary", "start", "end"],
                },
            },
            {
                "name": "gmail.list_messages",
                "description": "List Gmail messages.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer"},
                        "q": {"type": "string"},
                    },
                    "required": [],
                },
            },
            {
                "name": "gmail.list_drafts",
                "description": "List Gmail drafts.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        ]
    }


@router.post("/execute")
def tool_execute(
    payload: ToolExecuteRequest,
    x_tool_token: str | None = Header(default=None),
    x_internal_user_id: str | None = Header(default=None),
    x_internal_agent_id: str | None = Header(default=None),
    x_internal_timestamp: str | None = Header(default=None),
    x_internal_request: str | None = Header(default=None),
    x_internal_nonce: str | None = Header(default=None),
    x_internal_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _require_tool_token(x_tool_token)
    if x_internal_request != "true":
        logger.warning("tool_execute_signal", extra={"reason": "missing_internal_marker"})
    if not x_internal_user_id or not x_internal_user_id.isdigit():
        logger.warning("tool_execute_rejected", extra={"reason": "missing_user_context"})
        raise HTTPException(status_code=401, detail="Missing internal context")
    if not x_internal_timestamp or not x_internal_timestamp.isdigit():
        logger.warning("tool_execute_rejected", extra={"reason": "missing_timestamp"})
        raise HTTPException(status_code=401, detail="Missing internal timestamp")
    if not x_internal_nonce:
        logger.warning("tool_execute_rejected", extra={"reason": "missing_nonce"})
        raise HTTPException(status_code=401, detail="Missing nonce")

    internal_user_id = int(x_internal_user_id)
    internal_agent_id = int(x_internal_agent_id) if x_internal_agent_id and x_internal_agent_id.isdigit() else None
    internal_timestamp = int(x_internal_timestamp)
    _validate_nonce(x_internal_nonce)
    _validate_internal_signature(internal_user_id, payload, internal_timestamp, x_internal_nonce, x_internal_signature)

    return _execute_tool_internal(payload, db, internal_user_id, internal_agent_id)
