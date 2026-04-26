from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolDryRunLog(Base):
    __tablename__ = "tool_dry_run_log"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user_id = Column(Integer, nullable=True)
    agent_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    step_index = Column(Integer, nullable=True)
    tool_name = Column(String(200), nullable=False)
    args_redacted = Column(Text, nullable=True)
    simulated_result = Column(Text, nullable=True)
    status = Column(String(24), nullable=False, default="ok")
    reason = Column(String(500), nullable=True)
    meta_json = Column(Text, nullable=True)
