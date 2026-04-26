"""ChatService — Phase 2b of the unified chat refactor.

Single entry point used by both the web chat endpoint and the Telegram
webhook. This skeleton handles only two intents end-to-end:

  * general_chat  → LLM reply (no tools, no agent)
  * run_agent     → resolves an existing agent and dispatches to
                    `agent_runtime.execute_agent_run` (kept synchronous for
                    parity with the current Telegram /run flow).

All other intents return a friendly stub message indicating they are not
yet wired up. Subsequent phases (3, 4, 8) will fill in:

  * create_agent  → AgentBuilder.from_natural_language
  * modify_agent  → AgentEditor
  * tool_request  → PermissionService chat objects
  * list_agents / show_runs → simple DB queries

Design rules:
- Stateless service; takes a Session per call.
- Caller commits the transaction.
- Returns a structured `ChatResponse` (channel-agnostic) so adapters
  decide how to render (HTML card vs Telegram inline keyboard).
- Never raises into the caller for user-input errors — converts them to
  assistant messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ai_keys import get_default_provider, get_user_key
from app.core.config import settings
from app.core.llm_client import LLMError, call_gemini, call_openai_chat
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.user import User
from app.services.agent_builder import agent_builder
from app.services.agent_runtime import AgentRuntimeError, execute_agent_run
from app.services.permission_service import permission_service
from app.services.intent_router import (
    INTENT_CREATE_AGENT,
    INTENT_DELETE_AGENT,
    INTENT_GENERAL_CHAT,
    INTENT_LIST_AGENTS,
    INTENT_MODIFY_AGENT,
    INTENT_RUN_AGENT,
    INTENT_SHOW_RUNS,
    INTENT_TOOL_REQUEST,
    Intent,
    intent_router,
)
from app.services.memory_service import (
    CHANNEL_TELEGRAM,
    CHANNEL_WEB,
    ROLE_ASSISTANT,
    ROLE_USER,
    memory_service,
)


logger = logging.getLogger(__name__)


# ---- response data --------------------------------------------------------
@dataclass
class ChatResponse:
    """Channel-agnostic response from ChatService.handle_message.

    `text` is the assistant's reply rendered for the user.
    `intent` is the routed intent (for logging / UI badges).
    `actions` is a list of structured chat objects (e.g. permission_request
              cards) that channel adapters render natively.
    `data` carries arbitrary metadata (run_id, agent_id, etc.).
    """

    text: str
    intent: str = INTENT_GENERAL_CHAT
    actions: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    conversation_id: Optional[int] = None


# ---- module-private helpers -----------------------------------------------
def _resolve_agent(db: Session, user_id: int, ref: str) -> Optional[Agent]:
    """Resolve an agent by id (numeric) or by name (case-insensitive)."""
    if not ref:
        return None
    ref = ref.strip()
    if ref.isdigit():
        agent = db.get(Agent, int(ref))
        if agent and (agent.user_id is None or agent.user_id == user_id):
            return agent
    stmt = select(Agent).where(Agent.user_id == user_id)
    agents = list(db.execute(stmt).scalars().all())
    lower = ref.lower()
    for a in agents:
        if a.name.lower() == lower:
            return a
    for a in agents:
        if lower in a.name.lower():
            return a
    return None


def _render_run_result(agent: Agent, run) -> str:
    output = (run.output_text or "").strip() if hasattr(run, "output_text") else ""
    if not output:
        output = "(no output)"
    status = getattr(run, "status", "unknown")
    return f"✅ {agent.name} finished ({status}).\n\n{output}"


# --- Operator persona ------------------------------------------------------
# OpenClaw is an *agent automation platform*, not a chatbot. The base persona
# is intentionally strict: never list generic LLM abilities, always answer in
# terms of agents / automations / integrations, and finish capability or help
# style answers with one concrete CTA.
OPERATOR_BASE_PERSONA = (
    "You are OpenClaw — the operator console of an AI agent automation "
    "platform. You are NOT a general-purpose chatbot, NOT ChatGPT, and NOT "
    "an LLM concierge. Your job is to help the user create, run, and "
    "manage automation agents.\n\n"
    "HARD RULES:\n"
    "1. Never describe yourself as 'an AI', 'a large language model', "
    "'a virtual assistant', or list generic LLM abilities (translation, "
    "writing poems, answering trivia, etc.) as your features.\n"
    "2. When asked what you can do, what your features are, how this "
    "works, or for help, answer ONLY in terms of OpenClaw capabilities: "
    "creating agents, running agents, automating Gmail / Calendar / "
    "Telegram tasks, scheduled summaries, approvals, and tool "
    "integrations. End with one concrete next-step suggestion (e.g. "
    "'try: create an agent that summarizes my unread emails').\n"
    "3. Default to short replies — 1 to 3 sentences. Use bullets only "
    "when the user explicitly asks for a list or steps are required.\n"
    "4. Small tasks the user clearly wants done (translate this line, "
    "rewrite this sentence, summarize this paragraph) → just do them, "
    "then optionally add one short line: 'want me to turn this into "
    "an agent?'\n"
    "5. If a request needs an integration the user hasn't connected, "
    "say so plainly and point them to the Tools page or /tools.\n"
    "6. Tone: professional, concise, action-oriented. No filler.\n"
)


def _build_user_context(db: Session, user_id: int) -> str:
    """Render a short live-state block to inject into the system prompt.

    Keeps queries cheap (counts only) and silently degrades on error so a
    DB hiccup never blocks chat replies.
    """
    try:
        agent_count = db.execute(
            select(func.count(Agent.id)).where(Agent.user_id == user_id)
        ).scalar() or 0
    except Exception:  # noqa: BLE001 — never block chat on context lookup
        agent_count = 0

    google_connected = False
    try:
        from app.models.google_account import GoogleAccount  # local import

        google_connected = bool(
            db.execute(
                select(GoogleAccount.id).where(GoogleAccount.user_id == user_id).limit(1)
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        google_connected = False

    telegram_linked = False
    try:
        from app.models.telegram_link import TelegramLink  # local import

        telegram_linked = bool(
            db.execute(
                select(TelegramLink.id).where(TelegramLink.user_id == user_id).limit(1)
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        telegram_linked = False

    tools = []
    if google_connected:
        tools.extend(["Gmail", "Google Calendar"])
    if telegram_linked:
        tools.append("Telegram")
    tools_str = ", ".join(tools) if tools else "none yet"

    if agent_count == 0:
        bias = (
            "This user has 0 agents — bias every reply toward onboarding "
            "and first-agent creation."
        )
    elif agent_count <= 2:
        bias = (
            "This user is early-stage with a few agents — encourage "
            "running existing agents and creating one more."
        )
    else:
        bias = (
            "This user is a power user with several agents — skip basic "
            "onboarding, prefer shortcuts and direct actions."
        )

    return (
        "LIVE USER CONTEXT (use to personalize, do not quote verbatim):\n"
        f"- Agents owned: {agent_count}\n"
        f"- Connected tools: {tools_str}\n"
        f"- Guidance: {bias}\n"
    )


_ANTI_PATTERNS = (
    "as an ai",
    "as a large language model",
    "i am an ai",
    "i'm an ai",
    "i am a large language model",
    "i'm a large language model",
    "as a virtual assistant",
)


def _looks_like_generic_llm(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _ANTI_PATTERNS)


def _llm_reply(
    db: Session, user_id: int, history: list[dict], user_text: str
) -> str:
    """Operator-style general-chat LLM call. Uses admin/default provider."""
    provider = get_default_provider(db) or "openai"
    api_key = get_user_key(db, user_id, provider)
    if not api_key:
        return (
            "I can't reply right now — the platform's LLM key isn't configured. "
            "Please ask an admin to set it in the server environment."
        )

    user_context = _build_user_context(db, user_id)
    system_prompt = OPERATOR_BASE_PERSONA + "\n" + user_context

    messages = (
        [{"role": "system", "content": system_prompt}]
        + list(history)
        + [{"role": "user", "content": user_text}]
    )

    def _call() -> str:
        if provider == "gemini":
            # Gemini helper takes a flat prompt; flatten messages for now.
            prompt_parts = [f"{m['role']}: {m['content']}" for m in messages]
            text, _tokens = call_gemini(
                api_key, "gemini-2.5-flash", "\n".join(prompt_parts)
            )
            return (text or "").strip()
        text, _tokens = call_openai_chat(api_key, "gpt-4o-mini", messages)
        return (text or "").strip()

    try:
        reply = _call()
        # Soft guardrail: if the model still produced a generic-LLM reply,
        # retry once with a stricter nudge before giving up.
        if _looks_like_generic_llm(reply):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous reply sounded like a generic AI "
                        "assistant. Rewrite it as the OpenClaw operator: "
                        "answer only in terms of agents and automations, "
                        "never call yourself an AI, and keep it to 1-3 "
                        "short sentences with one concrete next step."
                    ),
                }
            )
            retry = _call()
            if retry:
                reply = retry
        return reply or "(no response)"
    except LLMError as exc:
        logger.warning(
            "chat_service_llm_error provider=%s error=%s", provider, exc,
            extra={"error": str(exc), "provider": provider},
        )
        return "Sorry — I couldn't reach the model right now. Please try again in a moment."


# ---- public service --------------------------------------------------------
class ChatService:
    """Single entry point for unified chat (web + Telegram)."""

    def __init__(self) -> None:
        self.router = intent_router

    # -------- main entry --------
    def handle_message(
        self,
        db: Session,
        *,
        user: User,
        text: str,
        channel: str,
        external_ref: Optional[str] = None,
    ) -> ChatResponse:
        """Process a single inbound user message and return a response.

        `channel` should be one of memory_service.CHANNEL_* constants.
        `external_ref` lets us bind a conversation to a Telegram chat_id
        (or a browser session id) so subsequent messages reuse it.
        """
        text = (text or "").strip()

        # Resolve / create the conversation up-front so every branch logs into it.
        conversation = memory_service.get_or_create_conversation(
            db,
            user_id=user.id,
            channel=channel,
            external_ref=external_ref,
        )

        # Persist the inbound user message immediately.
        memory_service.append_message(
            db, conversation=conversation, role=ROLE_USER, content=text
        )

        # Empty body → friendly nudge.
        if not text:
            response = ChatResponse(
                text="Hi! Tell me what you'd like to do — for example "
                "“create an agent that summarizes my emails”.",
                intent=INTENT_GENERAL_CHAT,
                conversation_id=conversation.id,
            )
            self._record_assistant(db, conversation, response)
            return response

        intent = self.router.detect(text)
        logger.info(
            "chat_service_intent",
            extra={
                "user_id": user.id,
                "channel": channel,
                "intent": intent.name,
                "confidence": intent.confidence,
                "rule": intent.matched_rule,
            },
        )

        handler = self._handler_for(intent.name)
        try:
            response = handler(db, user, text, intent, conversation)
        except Exception:  # noqa: BLE001 — surface as graceful chat reply
            logger.exception(
                "chat_service_handler_error",
                extra={"user_id": user.id, "intent": intent.name},
            )
            response = ChatResponse(
                text="Something went wrong on my side. Please try again.",
                intent=intent.name,
                conversation_id=conversation.id,
            )

        response.conversation_id = conversation.id
        response.intent = response.intent or intent.name
        self._record_assistant(db, conversation, response)
        return response

    # -------- intent dispatch --------
    def _handler_for(self, intent_name: str):
        return {
            INTENT_GENERAL_CHAT: self._handle_general_chat,
            INTENT_RUN_AGENT: self._handle_run_agent,
            INTENT_CREATE_AGENT: self._handle_create_agent,
            INTENT_MODIFY_AGENT: self._handle_modify_agent,
            INTENT_DELETE_AGENT: self._handle_delete_agent,
            INTENT_LIST_AGENTS: self._handle_list_agents,
            INTENT_SHOW_RUNS: self._handle_show_runs,
            INTENT_TOOL_REQUEST: self._handle_tool_request,
        }.get(intent_name, self._handle_general_chat)

    # -------- handlers --------
    def _handle_general_chat(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        history_rows = memory_service.recent_messages(
            db, conversation_id=conversation.id, limit=settings.agent_memory_steps * 2
        )
        # Drop the just-inserted user message so we don't double-count it.
        history = memory_service.render_for_llm(history_rows[:-1])
        reply = _llm_reply(db, user.id, history, text)
        return ChatResponse(text=reply, intent=INTENT_GENERAL_CHAT)

    def _handle_run_agent(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        agent_ref = intent.slots.get("agent_ref")
        prompt = intent.slots.get("prompt") or text

        if not agent_ref and conversation.agent_id:
            agent = db.get(Agent, conversation.agent_id)
        else:
            agent = _resolve_agent(db, user.id, agent_ref or "") if agent_ref else None

        if agent is None:
            # Show a picker instead of asking the user to remember a name.
            agents = list(
                db.execute(
                    select(Agent)
                    .where(Agent.user_id == user.id)
                    .order_by(Agent.name.asc())
                )
                .scalars()
                .all()
            )
            if not agents:
                return ChatResponse(
                    text=(
                        "You don't have any agents yet. "
                        "Try: <i>create an agent that summarizes my unread emails</i>."
                    ),
                    intent=INTENT_RUN_AGENT,
                )
            return ChatResponse(
                text="Which agent should I run? Pick one:",
                intent=INTENT_RUN_AGENT,
                actions=[
                    {
                        "type": "agent_picker",
                        "action": "run",
                        "prompt": "Pick an agent to run",
                        "agents": [{"id": a.id, "name": a.name} for a in agents],
                    }
                ],
            )

        try:
            run = execute_agent_run(
                db, agent, user.id, prompt, source=f"chat:{conversation.channel}"
            )
        except AgentRuntimeError as exc:
            return ChatResponse(
                text=f"⚠️ Couldn't run {agent.name}: {exc}",
                intent=INTENT_RUN_AGENT,
            )

        memory_service.bind_agent(db, conversation, agent.id)
        return ChatResponse(
            text=_render_run_result(agent, run),
            intent=INTENT_RUN_AGENT,
            data={"agent_id": agent.id, "run_id": run.id},
        )

    def _handle_create_agent(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        # Gate behind sub-flag so this can be rolled out independently of
        # the master unified chat switch.
        if not (
            settings.unified_chat_nl_agent_builder_enabled
            or settings.unified_chat_enabled
        ):
            return ChatResponse(
                text=(
                    "Natural-language agent creation isn't enabled on this "
                    "deployment yet. Use the “New agent” button in the UI for now."
                ),
                intent=INTENT_CREATE_AGENT,
            )

        result = agent_builder.from_natural_language(db, user=user, text=text)
        data = {}
        actions: list[dict] = []
        if result.agent_id:
            data["agent_id"] = result.agent_id
            # Bind the conversation to the new agent so follow-up "run it"
            # messages don't need a name reference.
            memory_service.bind_agent(db, conversation, result.agent_id)
        if result.missing_tools and (
            settings.unified_chat_permission_cards_enabled
            or settings.unified_chat_enabled
        ):
            permission_objects = permission_service.request_many(
                db,
                user=user,
                tool_names=result.missing_tools,
                reason=f"Required by agent '{result.spec.name if result.spec else 'new agent'}'",
            )
            actions = [pr.to_dict() for pr in permission_objects]
            data["missing_tools"] = result.missing_tools
        elif result.missing_tools:
            data["missing_tools"] = result.missing_tools
        return ChatResponse(
            text=result.text,
            intent=INTENT_CREATE_AGENT,
            actions=actions,
            data=data,
        )

    # Phase 8: modify an existing agent via natural language.
    def _handle_modify_agent(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        if not (
            settings.unified_chat_nl_agent_builder_enabled
            or settings.unified_chat_enabled
        ):
            return ChatResponse(
                text=(
                    "Editing agents from chat isn't enabled on this deployment "
                    "yet. Use the agent dashboard for now."
                ),
                intent=INTENT_MODIFY_AGENT,
            )

        agent_ref = (intent.slots or {}).get("agent_ref")
        result = agent_builder.modify(db, user=user, text=text, agent_ref=agent_ref)
        data: dict = {}
        if result.agent_id:
            data["agent_id"] = result.agent_id
            memory_service.bind_agent(db, conversation, result.agent_id)
        return ChatResponse(
            text=result.text,
            intent=INTENT_MODIFY_AGENT,
            data=data,
        )

    # Phase 8b: delete an existing agent. Always confirm via inline picker
    # / button — never delete on the first turn — so a hallucinated name
    # match can't destroy the user's agent.
    def _handle_delete_agent(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        agents = list(
            db.execute(
                select(Agent).where(Agent.user_id == user.id).order_by(Agent.name.asc())
            )
            .scalars()
            .all()
        )
        if not agents:
            return ChatResponse(
                text="You don't have any agents to delete.",
                intent=INTENT_DELETE_AGENT,
            )

        agent_ref = (intent.slots or {}).get("agent_ref")
        target = _resolve_agent(db, user.id, agent_ref or "") if agent_ref else None

        if target is None:
            # Render an agent picker (Telegram → inline keyboard, web → buttons).
            return ChatResponse(
                text="Which agent should I delete? Pick one:",
                intent=INTENT_DELETE_AGENT,
                actions=[
                    {
                        "type": "agent_picker",
                        "action": "delete",
                        "prompt": "Pick an agent to delete",
                        "agents": [{"id": a.id, "name": a.name} for a in agents],
                    }
                ],
            )

        # Confirm before destructive action.
        return ChatResponse(
            text=f"⚠️ Delete <b>{target.name}</b>? This cannot be undone.",
            intent=INTENT_DELETE_AGENT,
            actions=[
                {
                    "type": "agent_delete_confirm",
                    "agent_id": target.id,
                    "agent_name": target.name,
                }
            ],
            data={"agent_id": target.id},
        )

    def _handle_list_agents(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        agents = list(
            db.execute(
                select(Agent).where(Agent.user_id == user.id).order_by(Agent.name.asc())
            )
            .scalars()
            .all()
        )
        if not agents:
            return ChatResponse(
                text="You don't have any agents yet. Say “create an agent that …” to start.",
                intent=INTENT_LIST_AGENTS,
            )
        lines = [f"• {a.name} — {a.role}" for a in agents]
        return ChatResponse(
            text="Your agents:\n" + "\n".join(lines), intent=INTENT_LIST_AGENTS
        )

    def _handle_not_yet_wired(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        return ChatResponse(
            text=(
                f"I understood “{intent.name}” but that flow isn't wired up yet "
                "(coming in the next phase). For now, try running an existing agent."
            ),
            intent=intent.name,
        )

    def _handle_show_runs(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        # S2: last-N runs, optionally scoped to a referenced agent.
        agent_ref = (intent.slots or {}).get("agent_ref")
        agent = _resolve_agent(db, user.id, agent_ref) if agent_ref else None
        if agent is None and conversation.agent_id:
            agent = db.get(Agent, conversation.agent_id)

        if agent is not None:
            stmt = (
                select(AgentRun)
                .where(AgentRun.user_id == user.id, AgentRun.agent_id == agent.id)
                .order_by(AgentRun.created_at.desc())
                .limit(10)
            )
        else:
            stmt = (
                select(AgentRun)
                .where(AgentRun.user_id == user.id)
                .order_by(AgentRun.created_at.desc())
                .limit(10)
            )
        rows = list(db.execute(stmt).scalars().all())
        if not rows:
            scope = f" for {agent.name}" if agent else ""
            return ChatResponse(text=f"No runs yet{scope}.", intent=INTENT_SHOW_RUNS)

        agent_ids = {r.agent_id for r in rows if r.agent_id}
        names: dict[int, str] = {}
        if agent_ids:
            agents = (
                db.execute(select(Agent).where(Agent.id.in_(agent_ids))).scalars().all()
            )
            names = {a.id: a.name for a in agents}
        lines = []
        for r in rows:
            when = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            who = names.get(r.agent_id, f"agent#{r.agent_id}")
            lines.append(f"• #{r.id} {who} — {r.status} ({when})")
        header = (
            f"Last {len(rows)} run(s)" + (f" for {agent.name}" if agent else "") + ":"
        )
        return ChatResponse(
            text=header + "\n" + "\n".join(lines), intent=INTENT_SHOW_RUNS
        )

    def _handle_tool_request(
        self, db: Session, user: User, text: str, intent: Intent, conversation
    ) -> ChatResponse:
        if not (
            settings.unified_chat_permission_cards_enabled
            or settings.unified_chat_enabled
        ):
            return self._handle_not_yet_wired(db, user, text, intent, conversation)
        # Naive extraction: pull the tool keyword from the matched message.
        import re as _re

        m = _re.search(
            r"\b(gmail|google\s*calendar|calendar|google|telegram|slack|notion|github)\b",
            text,
            _re.IGNORECASE,
        )
        if not m:
            return ChatResponse(
                text="Which tool would you like to connect? (e.g. Gmail, Google Calendar)",
                intent=INTENT_TOOL_REQUEST,
            )
        tool_name = m.group(1).lower().replace(" ", "_")
        pr = permission_service.request(
            db,
            user=user,
            tool_name=tool_name,
            reason="Requested by user in chat.",
        )
        if pr.request_id == 0:
            return ChatResponse(
                text=f"You're already connected to {tool_name}.",
                intent=INTENT_TOOL_REQUEST,
            )
        return ChatResponse(
            text=f"To connect <b>{tool_name}</b>, choose one of the options below.",
            intent=INTENT_TOOL_REQUEST,
            actions=[pr.to_dict()],
        )
    # -------- persistence helper --------
    def _record_assistant(self, db: Session, conversation, response: ChatResponse) -> None:
        meta: dict[str, Any] = {}
        if response.actions:
            meta["actions"] = response.actions
        if response.data:
            meta["data"] = response.data
        memory_service.append_message(
            db,
            conversation=conversation,
            role=ROLE_ASSISTANT,
            content=response.text,
            intent=response.intent,
            metadata=meta or None,
        )


chat_service = ChatService()


__all__ = ["ChatService", "ChatResponse", "chat_service", "CHANNEL_WEB", "CHANNEL_TELEGRAM"]
