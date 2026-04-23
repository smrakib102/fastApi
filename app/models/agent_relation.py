from sqlalchemy import Column, DateTime, Integer
from sqlalchemy.sql import func

from app.db.base import Base


class AgentRelation(Base):
    __tablename__ = "agent_relations"

    id = Column(Integer, primary_key=True)
    parent_id = Column(Integer, nullable=False)
    child_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
