from app.db.session import engine
from app.db.base import Base
from app.models import agent, approval, employee, reminder, task, user, user_profile


def init_db() -> None:
    _ = (agent, approval, employee, reminder, task, user, user_profile)
    Base.metadata.create_all(bind=engine)
