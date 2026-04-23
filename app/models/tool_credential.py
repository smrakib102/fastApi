from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolCredential(Base):
    __tablename__ = "tool_credentials"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    tool_id = Column(Integer, nullable=False)
    secret = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
