from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class OAuthCredential(Base):
    __tablename__ = "oauth_credentials"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    provider = Column(String(32), nullable=False)
    provider_account_id = Column(String(255), nullable=False)
    account_email = Column(String(255), nullable=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String(32), nullable=True)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
