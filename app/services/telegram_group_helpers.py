"""Telegram group helpers — invite link + branded delivery + error detection.

Centralized so the agent builder, the summary worker, and any future
agent runtime path all produce consistent UX:

* `build_group_invite_link(bot_username)` → t.me deep link that opens
  Telegram's native "Add this bot to a group" dialog with the right
  admin rights pre-checked. The user still taps Confirm — Telegram
  doesn't allow bots to invite themselves.

* `branded_summary(agent_name, body)` → wraps a summary with a small
  header so the message visually feels like it's from the named agent
  even though the underlying account is the platform bot.

* `is_bot_not_in_chat_error(response)` → returns True for the canonical
  Telegram errors raised when the bot can't read a group.
"""

from __future__ import annotations

from typing import Any


# Telegram-supported admin rights flags. We request only what's valid
# for *groups* — `post_messages` / `edit_messages` are channel-only and
# cause Telegram to throw "Could not add user" when used with a group.
# Users can edit the checkbox set in the popup before confirming.
_DEFAULT_GROUP_ADMIN_RIGHTS = "delete_messages+pin_messages+invite_users"


def build_group_invite_link(bot_username: str | None) -> str | None:
    """Return a one-tap t.me link that adds the bot to a group as admin.

    Returns None if the bot username isn't configured — caller should
    degrade to plain instructions in that case.
    """
    if not bot_username:
        return None
    uname = bot_username.lstrip("@")
    return f"https://t.me/{uname}?startgroup=true&admin={_DEFAULT_GROUP_ADMIN_RIGHTS}"


def branded_summary(agent_name: str | None, body: str, *, kind: str = "Daily summary") -> str:
    """Prepend an agent-branded header to a summary message body.

    The body is assumed to be pre-formatted (HTML, since Telegram is sent
    with parse_mode=HTML). If `agent_name` is empty we just return the
    body unchanged — no awkward "🤖 None" header.
    """
    name = (agent_name or "").strip()
    if not name:
        return body
    return f"🤖 <b>{name}</b> · <i>{kind}</i>\n\n{body}"


_BOT_NOT_IN_CHAT_HINTS = (
    "bot was kicked",
    "bot is not a member",
    "chat not found",
    "group chat was deactivated",
    "user_is_blocked",
    "forbidden",  # broad catch-all, Telegram uses 403 for most of these
)


def is_bot_not_in_chat_error(response: Any) -> bool:
    """True if the Telegram API response indicates we can't reach the chat.

    Accepts either a parsed response dict (from Bot API JSON) or a plain
    string description.
    """
    if response is None:
        return False
    if isinstance(response, dict):
        if response.get("ok") is True:
            return False
        description = str(response.get("description") or "").lower()
    else:
        description = str(response).lower()
    return any(hint in description for hint in _BOT_NOT_IN_CHAT_HINTS)


__all__ = [
    "build_group_invite_link",
    "branded_summary",
    "is_bot_not_in_chat_error",
]
