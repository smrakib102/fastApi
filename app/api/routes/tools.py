import base64
import json
from email.message import EmailMessage

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_legacy_user
from app.api.routes.google_oauth import _ensure_token, _get_default_account
from app.core.config import settings
from app.models.approval import Approval

router = APIRouter()

GMAIL_DRAFT_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class ToolExecuteRequest(BaseModel):
    name: str
    arguments: dict = {}


def _require_tool_token(x_tool_token: str | None) -> None:
    if settings.tool_api_token and x_tool_token != settings.tool_api_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_raw_message(to: str, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw_bytes = message.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


def _gmail_draft(args: dict, db: Session) -> dict:
    to = args.get("to")
    subject = args.get("subject")
    body = args.get("body")
    thread_id = args.get("thread_id")

    if not to or not subject or not body:
        raise HTTPException(status_code=400, detail="Missing to/subject/body")

    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}

    payload: dict = {"message": {"raw": _build_raw_message(to, subject, body)}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    response = httpx.post(GMAIL_DRAFT_URL, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to create draft")

    return response.json()


def _gmail_send_request(args: dict, db: Session) -> dict:
    draft_id = args.get("draft_id")
    reason = args.get("reason")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    legacy_user = get_legacy_user(db)
    approval = Approval(
        user_id=legacy_user.id,
        type="gmail.send",
        payload=json.dumps({"draft_id": draft_id, "reason": reason}),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)

    return {"approval_id": approval.id, "status": approval.status}


def _gmail_send(args: dict, db: Session) -> dict:
    draft_id = args.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.post(GMAIL_SEND_URL, headers=headers, json={"id": draft_id}, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to send draft")

    return response.json()


def _gmail_profile(db: Session) -> dict:
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(GMAIL_PROFILE_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch Gmail profile")
    return response.json()


def _calendar_list(db: Session) -> dict:
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    response = httpx.get(CALENDAR_LIST_URL, headers=headers, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch calendar list")
    return response.json()


def _gmail_list_messages(args: dict, db: Session) -> dict:
    max_results = args.get("max_results", 10)
    query = args.get("q")
    account = _ensure_token(db, _get_default_account(db))
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


def _gmail_list_drafts(args: dict, db: Session) -> dict:
    max_results = args.get("max_results", 10)
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    params = {"maxResults": max_results}
    response = httpx.get(GMAIL_DRAFT_URL, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to list drafts")
    return response.json()


def _calendar_create_request(args: dict, db: Session) -> dict:
    calendar_id = args.get("calendar_id")
    summary = args.get("summary")
    start = args.get("start")
    end = args.get("end")
    if not calendar_id or not summary or not start or not end:
        raise HTTPException(status_code=400, detail="Missing calendar fields")

    legacy_user = get_legacy_user(db)
    approval = Approval(
        user_id=legacy_user.id,
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
                    "properties": {"draft_id": {"type": "string"}},
                    "required": ["draft_id"],
                },
            },
            {
                "name": "gmail.profile",
                "description": "Fetch Gmail profile data.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "calendar.list",
                "description": "List Google calendars.",
                "input_schema": {"type": "object", "properties": {}},
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
                },
            },
            {
                "name": "gmail.list_drafts",
                "description": "List Gmail drafts.",
                "input_schema": {
                    "type": "object",
                    "properties": {"max_results": {"type": "integer"}},
                },
            },
        ]
    }


@router.post("/execute")
def tool_execute(
    payload: ToolExecuteRequest,
    x_tool_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    _require_tool_token(x_tool_token)

    if payload.name == "gmail.draft":
        return _gmail_draft(payload.arguments, db)
    if payload.name == "gmail.send_request":
        return _gmail_send_request(payload.arguments, db)
    if payload.name == "gmail.send":
        return _gmail_send(payload.arguments, db)
    if payload.name == "gmail.profile":
        return _gmail_profile(db)
    if payload.name == "calendar.list":
        return _calendar_list(db)
    if payload.name == "calendar.create_request":
        return _calendar_create_request(payload.arguments, db)
    if payload.name == "gmail.list_messages":
        return _gmail_list_messages(payload.arguments, db)
    if payload.name == "gmail.list_drafts":
        return _gmail_list_drafts(payload.arguments, db)

    raise HTTPException(status_code=404, detail="Unknown tool")
