"""Agent deletion service.

Hard-deletes an agent and all its child rows (runs, run steps, performance
metrics, approvals, team mappings) in a single transaction. Caller is
responsible for committing.

We hard-delete (rather than soft-delete) for v1 because the existing
list/insights queries don't filter by an `archived_at` column, so a soft
delete would still leave the agent visible in the UI — which is exactly
the bug we're fixing.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.agent_performance import AgentPerformance
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.models.approval import Approval
from app.models.conversation import Conversation
from app.models.team_agent import TeamAgent
from app.models.tool_performance import ToolPerformance

logger = logging.getLogger(__name__)


def delete_agent_cascade(db: Session, *, user_id: int, agent_id: int) -> Optional[str]:
    """Delete an agent owned by ``user_id`` and all child rows.

    Returns the deleted agent's name on success, or None if the agent
    does not exist or is not owned by the user. Caller must commit.
    """
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
    ).scalar_one_or_none()
    if agent is None:
        return None

    name = agent.name

    # Children that hold a hard FK to agents.id must go first.
    run_ids = list(
        db.execute(select(AgentRun.id).where(AgentRun.agent_id == agent.id))
        .scalars()
        .all()
    )
    if run_ids:
        db.query(AgentRunStep).filter(AgentRunStep.run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
    db.query(AgentRun).filter(AgentRun.agent_id == agent.id).delete(
        synchronize_session=False
    )
    db.query(AgentPerformance).filter(AgentPerformance.agent_id == agent.id).delete(
        synchronize_session=False
    )
    db.query(ToolPerformance).filter(ToolPerformance.agent_id == agent.id).delete(
        synchronize_session=False
    )
    db.query(Approval).filter(Approval.agent_id == agent.id).delete(
        synchronize_session=False
    )
    # team_agents has no FK constraint but we still want to clean it up.
    db.query(TeamAgent).filter(TeamAgent.agent_id == agent.id).delete(
        synchronize_session=False
    )
    # conversations.agent_id is nullable — unbind rather than delete the
    # conversation history so the user keeps their chat record.
    db.query(Conversation).filter(Conversation.agent_id == agent.id).update(
        {Conversation.agent_id: None}, synchronize_session=False
    )

    db.delete(agent)
    db.flush()
    logger.info(
        "agent_deleted",
        extra={"user_id": user_id, "agent_id": agent_id, "agent_name": name},
    )
    return name


__all__ = ["delete_agent_cascade"]
