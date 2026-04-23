from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.usage_log import UsageLog
from app.models.user import User
from app.models.user_limit import UserLimit

router = APIRouter()


class UsageRecord(BaseModel):
    provider: str
    tokens: int


def _sum_usage(db: Session, user_id: int, provider: str, since: datetime) -> int:
    total = db.execute(
        select(func.coalesce(func.sum(UsageLog.tokens), 0)).where(
            UsageLog.user_id == user_id,
            UsageLog.provider == provider,
            UsageLog.created_at >= since,
        )
    ).scalar_one()
    return int(total or 0)


@router.post("/record")
def record_usage(
    payload: UsageRecord,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    limit = db.execute(
        select(UserLimit).where(
            UserLimit.user_id == current_user.id, UserLimit.provider == payload.provider
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if limit:
        if limit.daily_limit is not None:
            daily_total = _sum_usage(db, current_user.id, payload.provider, now - timedelta(days=1))
            if daily_total + payload.tokens > limit.daily_limit:
                raise HTTPException(status_code=429, detail="Daily usage limit exceeded")
        if limit.monthly_limit is not None:
            monthly_total = _sum_usage(db, current_user.id, payload.provider, now - timedelta(days=30))
            if monthly_total + payload.tokens > limit.monthly_limit:
                raise HTTPException(status_code=429, detail="Monthly usage limit exceeded")

    db.add(UsageLog(user_id=current_user.id, provider=payload.provider, tokens=payload.tokens))
    db.commit()
    return {"ok": True}
