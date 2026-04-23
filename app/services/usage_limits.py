from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.usage_log import UsageLog
from app.models.user_limit import UserLimit


def _sum_usage(db: Session, user_id: int, provider: str, since: datetime) -> int:
    total = db.execute(
        select(func.coalesce(func.sum(UsageLog.tokens), 0)).where(
            UsageLog.user_id == user_id,
            UsageLog.provider == provider,
            UsageLog.created_at >= since,
        )
    ).scalar_one()
    return int(total or 0)


def check_and_record_usage(db: Session, user_id: int, provider: str, tokens: int) -> None:
    limit = db.execute(
        select(UserLimit).where(UserLimit.user_id == user_id, UserLimit.provider == provider)
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if limit:
        if limit.daily_limit is not None:
            daily_total = _sum_usage(db, user_id, provider, now - timedelta(days=1))
            if daily_total + tokens > limit.daily_limit:
                raise HTTPException(status_code=429, detail="Daily usage limit exceeded")
        if limit.monthly_limit is not None:
            monthly_total = _sum_usage(db, user_id, provider, now - timedelta(days=30))
            if monthly_total + tokens > limit.monthly_limit:
                raise HTTPException(status_code=429, detail="Monthly usage limit exceeded")

    db.add(UsageLog(user_id=user_id, provider=provider, tokens=tokens))
    db.commit()
