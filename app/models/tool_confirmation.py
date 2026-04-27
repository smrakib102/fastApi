from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolConfirmation(Base):
    __tablename__ = "tool_confirmations"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), nullable=False)
    user_id = Column(Integer, nullable=False)
    agent_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    step_index = Column(Integer, nullable=True)
    tool_name = Column(String(200), nullable=False)
    args_redacted = Column(Text, nullable=True)
    meta_json = Column(Text, nullable=True)
    status = Column(String(24), nullable=False, default="pending")
    reason = Column(String(500), nullable=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String(120), nullable=True)
