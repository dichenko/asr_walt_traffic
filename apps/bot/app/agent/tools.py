"""LangChain tool definitions for the Walt Traffic agent."""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import EscalationRepository
from app.services.admin_notify import send_admin_notification


def _get_config(config: RunnableConfig) -> dict[str, Any]:
    return config.get("configurable", {})


def _get_session(config: RunnableConfig) -> AsyncSession:
    return _get_config(config)["session"]


def _get_user(config: RunnableConfig) -> Any:
    return _get_config(config)["user"]


def _get_admin_bot(config: RunnableConfig) -> Any | None:
    return _get_config(config).get("admin_bot")


def _mark_side_effect(config: RunnableConfig, tool_name: str) -> None:
    tracker = _get_config(config).setdefault(
        "side_effects",
        {"executed": False, "tools": []},
    )
    tracker["executed"] = True
    tools = tracker.setdefault("tools", [])
    if tool_name not in tools:
        tools.append(tool_name)


class SendToAdminInput(BaseModel):
    summary: str = Field(description="Short summary of the user's request")
    contact: str | None = Field(
        default=None,
        description="User contact, phone number, or Telegram username if available",
    )
    urgency: str = Field(
        default="normal",
        description="Urgency level: low, normal, or high",
    )


@tool(args_schema=SendToAdminInput)
async def send_to_admin(
    summary: str,
    contact: str | None = None,
    urgency: str = "normal",
    config: RunnableConfig = None,
) -> str:
    """Send the user's request to an administrator."""
    session = _get_session(config)
    user = _get_user(config)
    admin_bot = _get_admin_bot(config)
    telegram_contact = (
        f"@{user.telegram_username}"
        if getattr(user, "telegram_username", None)
        else f"tg://user?id={user.telegram_user_id}"
    )
    contact_text = contact or telegram_contact

    _mark_side_effect(config, "send_to_admin")
    escalation = await EscalationRepository(session).create(
        user_id=user.id,
        reason=f"admin_request_{urgency}",
        summary=summary,
        phone=contact_text,
    )
    notification = await send_admin_notification(
        bot=admin_bot,
        message_text="\n".join(
            [
                "Admin request",
                "",
                f"Escalation ID: {escalation.id}",
                f"Urgency: {urgency}",
                f"Contact: {contact_text}",
                f"Telegram user ID: {user.telegram_user_id}",
                "",
                "Summary:",
                summary,
            ]
        ),
    )
    if notification.admin_chat_id is not None:
        escalation.admin_chat_id = notification.admin_chat_id
    if notification.admin_message_id is not None:
        escalation.admin_message_id = notification.admin_message_id
    await session.flush()

    if notification.sent:
        return "Request sent to an administrator."
    return "Request saved for an administrator."


ALL_TOOLS = [send_to_admin]
