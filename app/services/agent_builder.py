"""AgentBuilder — Phase 3 of the unified chat refactor.

Translates a free-form natural language description into a structured
Agent specification, then persists it. Replaces the multi-step web
wizard and the Telegram template picker for the unified-chat path.

Input:  one user sentence, e.g.
        "Create an agent that summarizes my emails every morning."

Output: ChatResponse-compatible result. Either:
  * created: the Agent row was persisted, returns confirmation text.
  * needs_clarification: the builder couldn't fill a required slot,
    returns a single short clarifying question.

Design rules:
- Reuses the existing LLM client (no new infra).
- Falls back to deterministic defaults when LLM is unavailable so the
  flow degrades gracefully.
- Tools are filtered against the existing ToolRegistry (admin-enabled
  global tools first, then user-scoped). Unknown tool names are dropped
  from the spec (the spec keeps a `requested_tools` list so future
  PermissionService can ask the user to enable missing ones).
- Does NOT touch any existing form-based create_agent code path.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ai_keys import get_default_provider, get_user_key
from app.core.llm_client import LLMError, call_gemini, call_openai_chat
from app.models.agent import Agent
from app.models.tool_registry import ToolRegistry
from app.models.user import User


logger = logging.getLogger(__name__)


# ---- spec dataclass --------------------------------------------------------
@dataclass
class AgentSpec:
    name: str
    role: str
    model: str = "auto"
    tools: list[str] = field(default_factory=list)
    requested_tools: list[str] = field(default_factory=list)  # raw LLM output, pre-filter
    category: str = "general"
    memory_required: bool = True
    workflow_type: str = "single_step"
    raw_description: str = ""

    def to_db_payload(self, user_id: int) -> dict:
        return {
            "user_id": user_id,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "tools": json.dumps(self.tools),
            "category": self.category,
            "status": "active",
            "config": json.dumps(
                {
                    "memory_required": self.memory_required,
                    "workflow_type": self.workflow_type,
                    "requested_tools": self.requested_tools,
                    "source": "nl_agent_builder",
                    "raw_description": self.raw_description,
                }
            ),
        }


@dataclass
class BuildResult:
    """Outcome of an AgentBuilder.from_natural_language() call."""

    status: str  # 'created' | 'needs_clarification' | 'failed'
    text: str
    spec: Optional[AgentSpec] = None
    agent_id: Optional[int] = None
    missing_tools: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)


# ---- prompt template -------------------------------------------------------
_BUILDER_SYSTEM = (
    "You are an agent specification generator for a multi-user AI agent platform.\n"
    "Given a single user instruction, output ONE JSON object describing the agent to create.\n"
    "Schema (all fields required):\n"
    "{\n"
    '  "name": "<2-4 word title-case name>",\n'
    '  "role": "<one short sentence describing what the agent does>",\n'
    '  "category": "<one of: general | email | calendar | research | automation | data | writing>",\n'
    '  "tools": [<tool names from the available list, or [] if none needed>],\n'
    '  "memory_required": <true|false>,\n'
    '  "workflow_type": "<single_step | scheduled | multi_step>"\n'
    "}\n"
    "Rules:\n"
    "- Output ONLY the JSON object, no prose, no code fences.\n"
    "- Pick tools ONLY from the provided list. If none fit, use [].\n"
    "- Keep `name` short and human-friendly.\n"
    "- Default `model` is handled by the platform; do not include it.\n"
)


_NAME_FALLBACK_STOPWORDS = {
    "create", "build", "make", "set", "up", "an", "a", "the", "agent",
    "bot", "assistant", "that", "which", "for", "me", "my", "to",
    "and", "or", "with", "on", "of",
}


def _safe_name_from_text(text: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]+", text)
    keep = [w for w in words if w.lower() not in _NAME_FALLBACK_STOPWORDS]
    if not keep:
        keep = words[:3]
    pick = keep[:3] or ["My", "Agent"]
    name = " ".join(w.capitalize() for w in pick)
    if "agent" not in name.lower():
        name = f"{name} Agent"
    return name[:80]


def _list_available_tools(db: Session, user_id: int) -> list[ToolRegistry]:
    """Return tools admin-enabled globally OR scoped to this user."""
    stmt = select(ToolRegistry).where(
        (ToolRegistry.is_global.is_(True)) | (ToolRegistry.user_id == user_id)
    )
    return list(db.execute(stmt).scalars().all())


def _format_tools_for_prompt(tools: list[ToolRegistry]) -> str:
    if not tools:
        return "(no tools available)"
    lines = []
    for t in tools[:40]:  # cap to keep prompt short
        desc = (t.description or "").strip().replace("\n", " ")[:120]
        lines.append(f"- {t.name}: {desc}" if desc else f"- {t.name}")
    return "\n".join(lines)


def _call_llm(db: Session, user_id: int, user_text: str, tools_block: str) -> Optional[dict]:
    provider = get_default_provider(db) or "openai"
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        return None

    user_prompt = (
        f"AVAILABLE TOOLS:\n{tools_block}\n\n"
        f"USER INSTRUCTION:\n{user_text}\n\n"
        "Return the JSON object now."
    )

    try:
        if provider == "gemini":
            text, _ = call_gemini(
                api_key,
                "gemini-2.5-flash",
                f"{_BUILDER_SYSTEM}\n\n{user_prompt}",
            )
        else:
            text, _ = call_openai_chat(
                api_key,
                "gpt-4o-mini",
                [
                    {"role": "system", "content": _BUILDER_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
            )
    except LLMError as exc:
        logger.warning("agent_builder_llm_error", extra={"error": str(exc)})
        return None

    return _extract_json(text or "")


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    # Strip optional code fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # Try to find the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _spec_from_llm(data: dict, user_text: str, available_tool_names: set[str]) -> AgentSpec:
    name = (data.get("name") or "").strip() or _safe_name_from_text(user_text)
    role = (data.get("role") or "").strip() or user_text.strip()[:200]
    category = (data.get("category") or "general").strip().lower() or "general"
    raw_tools = data.get("tools") or []
    if not isinstance(raw_tools, list):
        raw_tools = []
    requested = [str(t).strip() for t in raw_tools if isinstance(t, (str, int))]
    tools = [t for t in requested if t in available_tool_names]
    workflow = (data.get("workflow_type") or "single_step").strip()
    memory_required = bool(data.get("memory_required", True))

    return AgentSpec(
        name=name[:80],
        role=role[:300],
        model="auto",
        tools=tools,
        requested_tools=requested,
        category=category[:60],
        memory_required=memory_required,
        workflow_type=workflow[:40] if workflow in {"single_step", "scheduled", "multi_step"} else "single_step",
        raw_description=user_text,
    )


def _spec_fallback(user_text: str) -> AgentSpec:
    """Deterministic spec used when the LLM is unavailable."""
    name = _safe_name_from_text(user_text)
    return AgentSpec(
        name=name,
        role=user_text.strip()[:300] or "General assistant",
        model="auto",
        tools=[],
        requested_tools=[],
        category="general",
        memory_required=True,
        workflow_type="single_step",
        raw_description=user_text,
    )


def _ensure_unique_name(db: Session, base_name: str) -> str:
    name = base_name
    suffix = 2
    while db.execute(select(Agent).where(Agent.name == name)).scalar_one_or_none() is not None:
        name = f"{base_name} {suffix}"
        suffix += 1
        if suffix > 50:
            break
    return name


def _find_incomplete_agent(
    db: Session, user_id: int, proposed_name: str, spec: "AgentSpec", raw_text: str
) -> Agent | None:
    """Return the most recent agent owned by this user that matches the
    proposed name AND is in an incomplete setup state, else None.

    Today the only "incomplete" state we track is: a Telegram-group
    agent whose config has no ``telegram_chat_id`` bound. When that
    matches we ask the user whether to resume or discard — instead of
    silently renaming the new agent to "<name> 2".
    """
    import json as _json

    needs_group = _agent_needs_telegram_group(spec, raw_text)
    if not needs_group:
        return None

    candidates = (
        db.execute(
            select(Agent)
            .where(Agent.user_id == user_id, Agent.name.ilike(proposed_name))
            .order_by(Agent.created_at.desc())
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        try:
            cfg = _json.loads(candidate.config) if candidate.config else {}
        except Exception:  # noqa: BLE001
            cfg = {}
        if not cfg.get("telegram_chat_id"):
            return candidate
    return None


# ---- Telegram-group convenience helpers ----------------------------------
# Surfaced in the post-create confirmation when the agent looks like it
# needs to read or post in a Telegram group, so the user gets a one-tap
# "add me to the group" link instead of being told to dig through menus.

_TELEGRAM_GROUP_HINTS = (
    "telegram group",
    "telegram channel",
    "telegram chat",
    "group conversation",
    "group chat",
    "monitor my group",
    "watch my group",
    "track my group",
    "summari",  # "summarize", "summarise"
)


def _agent_needs_telegram_group(spec: AgentSpec, raw_text: str) -> bool:
    """Heuristic: does this agent need to be added to a Telegram group?

    We accept either signal — the LLM tagged a telegram tool, or the
    user's text/role obviously mentions a group. The downstream picker
    is cancellable, so a false positive just costs the user one tap.
    """
    blob = " ".join(
        [
            (raw_text or "").lower(),
            (spec.role or "").lower(),
            (spec.raw_description or "").lower(),
        ]
    )
    has_telegram_tool = any(
        "telegram" in (t or "").lower()
        for t in (spec.tools or []) + (spec.requested_tools or [])
    )
    mentions_group = any(hint in blob for hint in _TELEGRAM_GROUP_HINTS)
    return has_telegram_tool or mentions_group


def _telegram_group_setup_block(db: Session, user_id: int) -> str | None:
    """Render a one-tap invite link + short next-steps guide. None if the
    bot username isn't configured (so we don't print a broken link)."""
    # Local imports to avoid a top-level cycle: telegram.py imports the
    # builder via chat_service in the unified flow.
    from app.api.routes.telegram import _get_bot_username
    from app.services.telegram_group_helpers import build_group_invite_link

    bot_username = _get_bot_username(db, user_id)
    invite = build_group_invite_link(bot_username)
    if not invite:
        return None
    return (
        "<b>One last step</b>\n"
        f'<a href="{invite}">➕ Tap here to add me to your group as admin</a>\n'
        "Telegram requires a human admin to add me — this link opens the "
        "native Add-bot dialog with the right permissions pre-checked."
    )


def _build_telegram_group_picker_action(
    db: Session, user_id: int, agent_id: int, agent_name: str
) -> dict | None:
    """Return an action payload describing a tappable picker of the user's
    known Telegram groups + an "add me to a new group" deep link.

    Returns None if there's no bot username configured AND no known
    groups — caller should fall back to a plain instruction in that case.
    Channel adapters (currently only Telegram) translate this action
    into native UI (inline keyboard).
    """
    import json as _json

    from app.api.routes.telegram import _get_bot_username
    from app.models.telegram_message import TelegramMessage
    from app.services.telegram_group_helpers import build_group_invite_link

    bot_username = _get_bot_username(db, user_id)
    invite_url = build_group_invite_link(bot_username)

    # Distinct (chat_id, chat_type) pairs the user has been seen in
    # within their groups. Limit to the most recent ~10 to keep the
    # keyboard usable.
    rows = db.execute(
        select(
            TelegramMessage.chat_id,
            TelegramMessage.chat_type,
            TelegramMessage.raw_json,
        )
        .where(
            TelegramMessage.user_id == user_id,
            TelegramMessage.chat_type.in_(("group", "supergroup")),
        )
        .order_by(TelegramMessage.id.desc())
        .limit(200)
    ).all()

    seen: dict[str, str] = {}
    for chat_id, _chat_type, raw in rows:
        if chat_id in seen:
            continue
        title = chat_id  # fallback
        try:
            blob = _json.loads(raw or "{}")
            chat_blob = blob.get("chat") or blob.get("message", {}).get("chat") or {}
            title = chat_blob.get("title") or chat_id
        except Exception:  # noqa: BLE001
            pass
        seen[chat_id] = title
        if len(seen) >= 10:
            break

    groups = [{"chat_id": cid, "title": title} for cid, title in seen.items()]
    if not groups and not invite_url:
        return None

    return {
        "type": "telegram_group_picker",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "prompt": f"Which group should <b>{agent_name}</b> monitor?",
        "groups": groups,
        "invite_url": invite_url,
    }


# ---- public service --------------------------------------------------------
class AgentBuilder:
    """Stateless service. Caller commits the transaction."""

    def from_natural_language(
        self, db: Session, *, user: User, text: str
    ) -> BuildResult:
        text = (text or "").strip()
        if len(text) < 6:
            return BuildResult(
                status="needs_clarification",
                text="What should this agent do? Tell me in one sentence "
                "(e.g. “summarize my emails every morning”).",
            )

        available_tools = _list_available_tools(db, user.id)
        available_names = {t.name for t in available_tools}
        tools_block = _format_tools_for_prompt(available_tools)

        data = _call_llm(db, user.id, text, tools_block)
        if data is None:
            spec = _spec_fallback(text)
        else:
            spec = _spec_from_llm(data, text, available_names)

        # Auto-attach the built-in group-summary tool when the agent
        # obviously needs to read a Telegram group. The LLM tool list is
        # filtered against ToolRegistry, but this plugin lives outside
        # that registry — without this hook a summary agent would ship
        # with `tools: []` and refuse to do anything when run.
        if _agent_needs_telegram_group(spec, text):
            if "telegram.group_summary" not in spec.tools:
                spec.tools.append("telegram.group_summary")

        # ---- Resume incomplete prior creation? -----------------------------
        # If this user already has an agent with the same proposed name
        # and it's still incomplete (telegram-group agent without a bound
        # chat), don't silently rename to "<name> 2" — ask whether to
        # resume or discard. The user's reply (button tap) is handled by
        # the channel layer; we just emit the action.
        existing_incomplete = _find_incomplete_agent(db, user.id, spec.name, spec, text)
        if existing_incomplete is not None:
            return BuildResult(
                status="needs_clarification",
                text=(
                    f"⚠️ You already have an agent named <b>{existing_incomplete.name}</b> "
                    "that wasn't fully set up yet (no group is bound).\n\n"
                    "Do you want to continue setting it up, or delete it and start over?"
                ),
                actions=[
                    {
                        "type": "agent_resume_prompt",
                        "agent_id": existing_incomplete.id,
                        "agent_name": existing_incomplete.name,
                    }
                ],
            )

        # Persist
        spec.name = _ensure_unique_name(db, spec.name)
        agent = Agent(**spec.to_db_payload(user.id))
        db.add(agent)
        db.flush()

        missing = [t for t in spec.requested_tools if t not in available_names]

        confirmation = (
            f"✅ Created agent <b>{spec.name}</b>.\n"
            f"Role: {spec.role}\n"
            f"Tools: {', '.join(spec.tools) if spec.tools else 'none yet'}"
        )
        if missing:
            confirmation += (
                f"\n\nNote: I wanted to use {', '.join(missing)} but they aren't "
                "available yet. I'll request access in a follow-up step."
            )

        # If the agent involves a Telegram group source, surface a tappable
        # picker of groups the bot already sees, plus a one-tap "add me to a
        # new group" link. The user picks → we bind the chat_id and create
        # the schedule. This avoids the "type the chat name" dead-end.
        actions: list[dict] = []
        if _agent_needs_telegram_group(spec, text):
            picker_action = _build_telegram_group_picker_action(
                db, user.id, agent.id, agent.name
            )
            if picker_action:
                actions.append(picker_action)
                confirmation += (
                    "\n\n<b>One last step:</b> tap the group I should monitor "
                    "(or add me to a new one)."
                )
            else:
                # Fall back to the plain invite link if we couldn't build
                # the picker (e.g., bot username not configured).
                fallback = _telegram_group_setup_block(db, user.id)
                if fallback:
                    confirmation += "\n\n" + fallback

        if not actions:
            confirmation += f"\n\nSay “run {spec.name}” to start it."

        return BuildResult(
            status="created",
            text=confirmation,
            spec=spec,
            agent_id=agent.id,
            missing_tools=missing,
            actions=actions,
        )

    # ---- Phase 8: modify_agent ----------------------------------------
    # Deterministic, regex-driven edits to an existing agent. Keeping the
    # surface narrow avoids LLM-driven destructive changes in v1.
    def modify(
        self,
        db: Session,
        *,
        user: User,
        text: str,
        agent_ref: Optional[str] = None,
    ) -> BuildResult:
        text = (text or "").strip()
        if not text:
            return BuildResult(
                status="needs_clarification",
                text="What change do you want to make? "
                "Try “rename Email Bot to Inbox Helper” or “add tool gmail.draft to Email Bot”.",
            )

        # Resolve target agent name from slot or from the message itself.
        ref = (agent_ref or "").strip()
        if not ref:
            m = re.search(
                r"\b(?:to|on|for)\s+(?:my\s+)?(?:agent\s+)?([a-z0-9][\w \-]{1,40}?)\s*$",
                text,
                re.IGNORECASE,
            )
            if m:
                ref = m.group(1).strip()
        if not ref:
            return BuildResult(
                status="needs_clarification",
                text="Which agent should I edit? Mention it by name.",
            )

        agent = db.execute(
            select(Agent).where(
                Agent.user_id == user.id,
                Agent.name.ilike(ref),
            )
        ).scalar_one_or_none()
        if not agent:
            agent = db.execute(
                select(Agent).where(
                    Agent.user_id == user.id,
                    Agent.name.ilike(f"%{ref}%"),
                )
            ).scalar_one_or_none()
        if not agent:
            return BuildResult(
                status="failed",
                text=f"I couldn’t find an agent matching “{ref}”.",
            )

        changes: list[str] = []

        # rename to X
        m = re.search(r"\brename(?:\s+(?:it|to))?\s+(?:to\s+)?[\"']?([\w \-]{2,80}?)[\"']?\s*$", text, re.IGNORECASE)
        if m:
            new_name = m.group(1).strip()
            if new_name and new_name.lower() != agent.name.lower():
                unique = _ensure_unique_name(db, new_name)
                changes.append(f"name: {agent.name} → {unique}")
                agent.name = unique

        # add tool X / enable tool X
        for m in re.finditer(
            r"\b(?:add|enable|attach)\s+(?:the\s+)?tool\s+([\w\.\-]+)",
            text,
            re.IGNORECASE,
        ):
            tool_name = m.group(1).strip()
            current = json.loads(agent.tools or "[]") if agent.tools else []
            if tool_name not in current:
                current.append(tool_name)
                agent.tools = json.dumps(current)
                changes.append(f"+tool {tool_name}")

        # remove tool X / disable tool X
        for m in re.finditer(
            r"\b(?:remove|disable|drop|detach)\s+(?:the\s+)?tool\s+([\w\.\-]+)",
            text,
            re.IGNORECASE,
        ):
            tool_name = m.group(1).strip()
            current = json.loads(agent.tools or "[]") if agent.tools else []
            if tool_name in current:
                current = [t for t in current if t != tool_name]
                agent.tools = json.dumps(current)
                changes.append(f"-tool {tool_name}")

        # change role to X / set role to X
        m = re.search(
            r"\b(?:change|set|update)\s+(?:the\s+)?role\s+(?:to\s+)?[\"']?(.{4,300}?)[\"']?\s*$",
            text,
            re.IGNORECASE,
        )
        if m:
            new_role = m.group(1).strip()
            if new_role:
                changes.append("role updated")
                agent.role = new_role[:300]

        if not changes:
            return BuildResult(
                status="needs_clarification",
                text=(
                    f"Found <b>{agent.name}</b>. "
                    "What change should I make? Try: rename, add tool X, "
                    "remove tool X, or change role to ..."
                ),
                agent_id=agent.id,
            )

        db.add(agent)
        db.flush()
        return BuildResult(
            status="created",  # reuse 'created' so chat_service treats it as success
            text=f"✅ Updated <b>{agent.name}</b>:\n" + "\n".join(f"• {c}" for c in changes),
            agent_id=agent.id,
        )


agent_builder = AgentBuilder()


__all__ = ["AgentBuilder", "AgentSpec", "BuildResult", "agent_builder"]
