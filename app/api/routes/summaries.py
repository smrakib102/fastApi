from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.summary_schedule import SummarySchedule
from app.services.summary_service import generate_summary
from app.services.telegram_service import send_message

router = APIRouter()


@router.post("/schedule")
def create_schedule(
    chat_id: str = Form(...),
    timezone: str = Form("UTC"),
    send_hour: int = Form(18),
    send_minute: int = Form(0),
    active: bool = Form(True),
    current_user=Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    schedule = SummarySchedule(
        user_id=current_user.id,
        chat_id=chat_id,
        timezone=timezone,
        send_hour=send_hour,
        send_minute=send_minute,
        active=active,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return {"id": schedule.id}


@router.get("/schedules")
def list_schedules(
    current_user=Depends(require_user),
    db: Session = Depends(get_db),
):
    schedules = db.execute(
        select(SummarySchedule).where(SummarySchedule.user_id == current_user.id)
    ).scalars().all()
    return {
        "items": [
            {
                "id": schedule.id,
                "chat_id": schedule.chat_id,
                "timezone": schedule.timezone,
                "send_hour": schedule.send_hour,
                "send_minute": schedule.send_minute,
                "active": schedule.active,
            }
            for schedule in schedules
        ]
    }


@router.post("/summary_now")
def summary_now(
    chat_id: str = Form(...),
    timezone: str = Form("UTC"),
    current_user=Depends(require_user),
    db: Session = Depends(get_db),
):
    text = generate_summary(db, current_user.id, chat_id, timezone)
    send_message(db, chat_id, text)
    return {"ok": True, "summary": text}
