from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolRequest(Base):
    __tablename__ = "tool_requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    tool_name = Column(String(120), nullable=False)
    details = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
