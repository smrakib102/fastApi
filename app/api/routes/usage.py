from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_user
from app.models.user import User
from app.services.usage_limits import check_and_record_usage

router = APIRouter()


class UsageRecord(BaseModel):
    provider: str
    tokens: int


@router.post("/record")
def record_usage(
    payload: UsageRecord,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user),
):
    check_and_record_usage(db, current_user.id, payload.provider, payload.tokens)
    return {"ok": True}
