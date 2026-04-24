from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class AgentPerformance(Base):
    __tablename__ = "agent_performance"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", name="uq_agent_performance_user_agent"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    run_count = Column(Integer, nullable=False, default=0)
    success_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    success_rate = Column(Float, nullable=False, default=0.0)
    reliability_score = Column(Float, nullable=False, default=0.0)
    cost_efficiency = Column(Float, nullable=False, default=0.0)
    avg_cost_usd = Column(Float, nullable=False, default=0.0)
    avg_tokens = Column(Float, nullable=False, default=0.0)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
