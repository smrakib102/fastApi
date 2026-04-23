from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.db.base import Base


class SummarySchedule(Base):
    __tablename__ = "summary_schedules"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    chat_id = Column(String(64), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")
    send_hour = Column(Integer, nullable=False, default=18)
    send_minute = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
