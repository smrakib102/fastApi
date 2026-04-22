import base64
import json
from email.message import EmailMessage

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_legacy_user, require_admin
from app.api.routes.google_oauth import _ensure_token, _get_default_account
from app.models.approval import Approval

router = APIRouter()

GMAIL_DRAFT_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class DraftRequest(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: str | None = None


class SendRequest(BaseModel):
    draft_id: str
    reason: str | None = None


def _build_raw_message(payload: DraftRequest) -> str:
    message = EmailMessage()
    message["To"] = payload.to
    message["Subject"] = payload.subject
    message.set_content(payload.body)
    raw_bytes = message.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


@router.post("/draft", dependencies=[Depends(require_admin)])
def create_draft(payload: DraftRequest, db: Session = Depends(get_db)):
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}

    body = {"message": {"raw": _build_raw_message(payload)}}
    if payload.thread_id:
        body["message"]["threadId"] = payload.thread_id

    response = httpx.post(GMAIL_DRAFT_URL, headers=headers, json=body, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to create draft")

    return response.json()


@router.post("/send-request", dependencies=[Depends(require_admin)])
def create_send_request(payload: SendRequest, db: Session = Depends(get_db)):
    legacy_user = get_legacy_user(db)
    approval = Approval(
        user_id=legacy_user.id,
        type="gmail.send",
        payload=json.dumps({"draft_id": payload.draft_id, "reason": payload.reason}),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return {"approval_id": approval.id, "status": approval.status}


@router.get("/inbox", dependencies=[Depends(require_admin)])
def list_inbox(max_results: int = 10, q: str | None = None, db: Session = Depends(get_db)):
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    params: dict = {"maxResults": max_results}
    if q:
        params["q"] = q

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


@router.get("/drafts", dependencies=[Depends(require_admin)])
def list_drafts(max_results: int = 10, db: Session = Depends(get_db)):
    account = _ensure_token(db, _get_default_account(db))
    headers = {"Authorization": f"Bearer {account.access_token}"}
    params = {"maxResults": max_results}

    response = httpx.get(GMAIL_DRAFT_URL, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to list drafts")
    return response.json()
