"""IntentRouter — Phase 2a of the unified chat refactor.

Lightweight, deterministic rules-based intent classifier. Pure function:
takes a raw user message string, returns a structured Intent with a
confidence score and any extracted slots.

Why rules first:
- Zero LLM cost / latency on the hot path.
- Deterministic and unit-testable.
- Easy to reason about for early rollout.

Future upgrade path: when `unified_chat_enabled` is fully on, callers can
optionally fall back to an LLM classifier for low-confidence results.
This module exposes a stable `classify()` API so the upgrade is transparent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---- intent label constants ------------------------------------------------
INTENT_CREATE_AGENT = "create_agent"
INTENT_RUN_AGENT = "run_agent"
INTENT_MODIFY_AGENT = "modify_agent"
INTENT_DELETE_AGENT = "delete_agent"
INTENT_LIST_AGENTS = "list_agents"
INTENT_SHOW_RUNS = "show_runs"
INTENT_TOOL_REQUEST = "tool_request"
INTENT_GENERAL_CHAT = "general_chat"

ALL_INTENTS = (
    INTENT_CREATE_AGENT,
    INTENT_RUN_AGENT,
    INTENT_MODIFY_AGENT,
    INTENT_DELETE_AGENT,
    INTENT_LIST_AGENTS,
    INTENT_SHOW_RUNS,
    INTENT_TOOL_REQUEST,
    INTENT_GENERAL_CHAT,
)


@dataclass
class Intent:
    name: str
    confidence: float  # 0.0 - 1.0
    slots: dict = field(default_factory=dict)
    matched_rule: Optional[str] = None

    def is_confident(self, threshold: float = 0.6) -> bool:
        return self.confidence >= threshold


# ---- regex rules -----------------------------------------------------------
# Each tuple = (intent_name, confidence, compiled_pattern, rule_id)
# Patterns are intentionally generous; ChatService can confirm with the user
# when confidence is borderline.

_RULES: list[tuple[str, float, re.Pattern, str]] = [
    # --- create_agent ------------------------------------------------------
    (
        INTENT_CREATE_AGENT,
        0.95,
        re.compile(
            r"\b(create|build|make|set\s*up|spin\s*up|setup)\b.*\b(agent|bot|assistant|workflow)\b",
            re.IGNORECASE,
        ),
        "create.verb_object",
    ),
    (
        INTENT_CREATE_AGENT,
        0.9,
        re.compile(r"\b(i\s+(?:want|need|would\s+like))\b.*\b(agent|bot|assistant)\b", re.IGNORECASE),
        "create.want_agent",
    ),
    (
        INTENT_CREATE_AGENT,
        0.85,
        re.compile(r"\bnew\s+agent\b", re.IGNORECASE),
        "create.new_agent",
    ),
    (
        INTENT_CREATE_AGENT,
        0.8,
        re.compile(r"^/newagent\b", re.IGNORECASE),
        "create.slash_newagent",
    ),

    # --- modify_agent ------------------------------------------------------
    (
        INTENT_MODIFY_AGENT,
        0.9,
        re.compile(
            r"\b(update|modify|change|edit|rename|tweak|adjust)\b.*\b(agent|bot|assistant)\b",
            re.IGNORECASE,
        ),
        "modify.verb_object",
    ),
    (
        INTENT_MODIFY_AGENT,
        0.85,
        re.compile(r"\b(add|remove|enable|disable)\b.*\b(tool|capability|permission)\b", re.IGNORECASE),
        "modify.tool_change",
    ),
    (
        INTENT_MODIFY_AGENT,
        0.8,
        re.compile(r"\bmake\b.*\b(more|less)\s+(strict|creative|verbose|concise|cautious)\b", re.IGNORECASE),
        "modify.behavior",
    ),

    # --- delete_agent ------------------------------------------------------
    # IMPORTANT: must come before modify_agent rules so "delete the agent"
    # doesn't get swallowed by modify.verb_object (which doesn't include
    # the verb 'delete' but is permissive on object words).
    (
        INTENT_DELETE_AGENT,
        0.95,
        re.compile(r"^/delete\b", re.IGNORECASE),
        "delete.slash_delete",
    ),
    (
        INTENT_DELETE_AGENT,
        0.92,
        re.compile(
            r"\b(delete|remove|destroy|trash|drop|kill|uninstall)\b.*\b(agent|bot|assistant|workflow)\b",
            re.IGNORECASE,
        ),
        "delete.verb_object",
    ),
    (
        INTENT_DELETE_AGENT,
        0.85,
        re.compile(
            r"\b(delete|remove|destroy|trash|drop)\s+(?:my\s+)?[\"']?([a-z0-9][\w \-]{1,40})[\"']?\s*$",
            re.IGNORECASE,
        ),
        "delete.verb_name",
    ),

    # --- list_agents -------------------------------------------------------
    (
        INTENT_LIST_AGENTS,
        0.9,
        re.compile(r"\b(what|which|show|list)\b.*\b(agents?|bots?)\b.*\b(do\s+i\s+have|are\s+there)?\b", re.IGNORECASE),
        "list.agents_question",
    ),
    (
        INTENT_LIST_AGENTS,
        0.95,
        re.compile(r"^/agents?\b", re.IGNORECASE),
        "list.slash_agents",
    ),

    # --- show_runs ---------------------------------------------------------
    (
        INTENT_SHOW_RUNS,
        0.9,
        re.compile(r"\b(show|list|view|see)\b.*\b(last|recent|today'?s?|my)\b.*\bruns?\b", re.IGNORECASE),
        "runs.show_recent",
    ),
    (
        INTENT_SHOW_RUNS,
        0.85,
        re.compile(r"\bsummari[sz]e\b.*\bruns?\b", re.IGNORECASE),
        "runs.summarize",
    ),

    # --- run_agent ---------------------------------------------------------
    (
        INTENT_RUN_AGENT,
        0.95,
        re.compile(r"^/run\b", re.IGNORECASE),
        "run.slash_run",
    ),
    (
        INTENT_RUN_AGENT,
        0.85,
        re.compile(r"\b(run|execute|start|trigger|kick\s*off|launch)\b.*\b(agent|bot|workflow|my)\b", re.IGNORECASE),
        "run.verb_object",
    ),
    (
        INTENT_RUN_AGENT,
        0.7,
        re.compile(r"\bask\s+(my|the)\s+\w+\s+agent\b", re.IGNORECASE),
        "run.ask_my_agent",
    ),

    # --- tool_request ------------------------------------------------------
    (
        INTENT_TOOL_REQUEST,
        0.85,
        re.compile(r"\b(connect|link|authori[sz]e|grant|allow)\b.*\b(gmail|google|calendar|telegram|slack|notion|github)\b", re.IGNORECASE),
        "tool.connect_oauth",
    ),
    (
        INTENT_TOOL_REQUEST,
        0.8,
        re.compile(r"\b(give|grant)\b.*\b(access|permission)\b", re.IGNORECASE),
        "tool.grant_access",
    ),
]


# Keywords used to enrich extracted slots (very simple).
_AGENT_NAME_PATTERNS = [
    re.compile(r"\bmy\s+([a-z][a-z0-9_\- ]{1,40}?)\s+agent\b", re.IGNORECASE),
    re.compile(r"\bagent\s+(?:called|named)\s+\"?([a-z0-9_\- ]{1,40})\"?", re.IGNORECASE),
]


def _extract_slots(message: str, intent: str) -> dict:
    slots: dict = {}
    if intent in {INTENT_RUN_AGENT, INTENT_MODIFY_AGENT, INTENT_DELETE_AGENT}:
        for pat in _AGENT_NAME_PATTERNS:
            m = pat.search(message)
            if m:
                slots["agent_ref"] = m.group(1).strip()
                break
        # /run <agent_ref> <prompt>
        m = re.match(r"^/run\s+(\S+)\s*(.*)$", message, re.IGNORECASE)
        if m:
            slots["agent_ref"] = m.group(1)
            if m.group(2):
                slots["prompt"] = m.group(2).strip()
    if intent == INTENT_DELETE_AGENT and "agent_ref" not in slots:
        # /delete <name> ...
        m = re.match(r"^/delete\s+(.+)$", message, re.IGNORECASE)
        if m:
            slots["agent_ref"] = m.group(1).strip()
        else:
            # "delete <name>" or "delete agent <name>" or "delete the bot <name>"
            m = re.search(
                r"\b(?:delete|remove|destroy|trash|drop|kill|uninstall)\s+(?:my\s+|the\s+)?(?:agent\s+|bot\s+|assistant\s+|workflow\s+)?[\"']?([a-z0-9][\w \-]{1,60}?)[\"']?\s*$",
                message,
                re.IGNORECASE,
            )
            if m:
                slots["agent_ref"] = m.group(1).strip()
    return slots


def classify(message: str) -> Intent:
    """Classify a raw user message into a structured Intent.

    Returns INTENT_GENERAL_CHAT with confidence 1.0 for empty messages so
    callers can short-circuit to the LLM reply path safely.
    """
    if not message or not message.strip():
        return Intent(name=INTENT_GENERAL_CHAT, confidence=1.0, matched_rule="empty")

    text = message.strip()

    best: Optional[Intent] = None
    for intent_name, conf, pattern, rule_id in _RULES:
        if pattern.search(text):
            slots = _extract_slots(text, intent_name)
            candidate = Intent(
                name=intent_name,
                confidence=conf,
                slots=slots,
                matched_rule=rule_id,
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate

    if best is not None:
        return best

    # No rule matched — default to general chat with low confidence so future
    # LLM fallback can override.
    return Intent(name=INTENT_GENERAL_CHAT, confidence=0.4, matched_rule="default")


class IntentRouter:
    """Thin OO wrapper so future LLM-backed routers can plug in via the
    same interface (ChatService depends on this class, not the function).
    """

    def detect(self, message: str) -> Intent:
        return classify(message)


intent_router = IntentRouter()
