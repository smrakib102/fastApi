from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(120), nullable=False, unique=True)
    role = Column(String(120), nullable=False)
    model = Column(String(120), nullable=False)
    tools = Column(Text, nullable=False, default="[]")
    category = Column(String(80), nullable=False, default="general")
    template_id = Column(Integer, ForeignKey("agent_templates.id"), nullable=True)
    config = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="active")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
