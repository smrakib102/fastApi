from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False)
    step_index = Column(Integer, nullable=False)
    step_number = Column(Integer, nullable=True)
    kind = Column(String(32), nullable=False, default="plan")
    status = Column(String(32), nullable=False, default="pending")
    action_type = Column(String(32), nullable=True)
    thought = Column(Text, nullable=True)
    tool_name = Column(String(120), nullable=True)
    input_json = Column(Text, nullable=True)
    output_json = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
