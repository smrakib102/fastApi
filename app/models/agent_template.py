from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    category = Column(String(80), nullable=False, default="general")
    model = Column(String(120), nullable=False, default="auto")
    tools = Column(Text, nullable=False, default="[]")
    fields = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
