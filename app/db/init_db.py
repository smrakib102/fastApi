from app.db.session import engine
from app.db.base import Base
from app.models import (
    agent,
    agent_relation,
    admin_setting,
    approval,
    employee,
    reminder,
    team,
    team_agent,
    task,
    telegram_link,
    tool_credential,
    tool_registry,
    tool_request,
    usage_log,
    user,
    user_limit,
    user_profile,
)


def init_db() -> None:
    _ = (
        agent,
        agent_relation,
        admin_setting,
        approval,
        employee,
        reminder,
        team,
        team_agent,
        task,
        telegram_link,
        tool_credential,
        tool_registry,
        tool_request,
        usage_log,
        user,
        user_limit,
        user_profile,
    )
    Base.metadata.create_all(bind=engine)
