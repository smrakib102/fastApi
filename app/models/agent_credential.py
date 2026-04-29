from sqlalchemy import Column, DateTime, Integer, Text
from sqlalchemy.sql import func

from app.db.base import Base


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, nullable=False)
    credential_id = Column(Integer, nullable=False)
    required_scopes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
