from sqlalchemy import Column, DateTime, Integer
from sqlalchemy.sql import func

from app.db.base import Base


class TeamAgent(Base):
    __tablename__ = "team_agents"

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, nullable=False)
    agent_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
