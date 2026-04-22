import json

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.routes.google_oauth import _ensure_token, _get_default_account
from app.models.approval import Approval

router = APIRouter()

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send"
CALENDAR_EVENT_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"


class ApprovalResolution(BaseModel):
    resolved_by: str | None = None


@router.post("/{approval_id}/approve", dependencies=[Depends(require_admin)])
def approve(approval_id: int, payload: ApprovalResolution, db: Session = Depends(get_db)):
    approval = db.execute(
        select(Approval).where(Approval.id == approval_id)
    ).scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")

    if approval.status != "pending":
        return {"status": approval.status}

    if approval.type == "gmail.send":
        data = json.loads(approval.payload)
        draft_id = data.get("draft_id")
        if not draft_id:
            raise HTTPException(status_code=400, detail="Missing draft_id")

        account = _ensure_token(db, _get_default_account(db))
        headers = {"Authorization": f"Bearer {account.access_token}"}
        response = httpx.post(
            GMAIL_SEND_URL, headers=headers, json={"id": draft_id}, timeout=30
        )
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to send draft")

    if approval.type == "calendar.create":
        data = json.loads(approval.payload)
        calendar_id = data.get("calendar_id")
        if not calendar_id:
            raise HTTPException(status_code=400, detail="Missing calendar_id")

        account = _ensure_token(db, _get_default_account(db))
        headers = {"Authorization": f"Bearer {account.access_token}"}

        body = {
            "summary": data.get("summary"),
            "description": data.get("description"),
            "start": data.get("start"),
            "end": data.get("end"),
        }
        attendees = data.get("attendees")
        if attendees:
            body["attendees"] = attendees

        response = httpx.post(
            CALENDAR_EVENT_URL.format(calendar_id=calendar_id),
            headers=headers,
            json=body,
            timeout=30,
        )
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to create calendar event")

    approval.status = "approved"
    approval.resolved_by = payload.resolved_by
    db.add(approval)
    db.commit()

    return {"status": approval.status}


@router.post("/{approval_id}/reject", dependencies=[Depends(require_admin)])
def reject(approval_id: int, payload: ApprovalResolution, db: Session = Depends(get_db)):
    approval = db.execute(
        select(Approval).where(Approval.id == approval_id)
    ).scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")

    if approval.status != "pending":
        return {"status": approval.status}

    approval.status = "rejected"
    approval.resolved_by = payload.resolved_by
    db.add(approval)
    db.commit()

    return {"status": approval.status}
