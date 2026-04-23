from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ToolRegistry(Base):
    __tablename__ = "tools"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    category = Column(String(80), nullable=True)
    provider = Column(String(80), nullable=True)
    is_global = Column(Boolean, nullable=False, default=False)
    user_id = Column(Integer, nullable=True)
    auth_type = Column(String(40), nullable=True)
    required_fields = Column(Text, nullable=True)
    input_schema = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    endpoint = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
