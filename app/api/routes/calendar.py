import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_legacy_user, require_admin
from app.models.approval import Approval

router = APIRouter()


class CalendarEventRequest(BaseModel):
    calendar_id: str
    summary: str
    description: str | None = None
    start: dict
    end: dict
    attendees: list[dict] | None = None
    timezone: str | None = None


@router.post("/event-request", dependencies=[Depends(require_admin)])
def create_event_request(payload: CalendarEventRequest, db: Session = Depends(get_db)):
    legacy_user = get_legacy_user(db)
    approval = Approval(
        user_id=legacy_user.id,
        type="calendar.create",
        payload=json.dumps(payload.model_dump()),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return {"approval_id": approval.id, "status": approval.status}
