import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, User
from app.db.repositories import EscalationRepository
from app.graph.intents import classify_intent as classify_user_intent
from app.graph.state import BotState
from app.services.admin_notify import send_admin_notification
from app.services.clinic_knowledge import get_clinic_knowledge
from app.services.faq import generate_admin_faq_answer
from app.services.owner_sales import (
    handle_owner_sales_message,
    is_owner_sales_in_progress,
)
from app.telegram.texts import Language, text

logger = logging.getLogger(__name__)


def build_nodes(
    *,
    session: AsyncSession,
    user: User,
    conversation: Conversation,
    admin_bot: Any | None = None,
):
    async def load_user_context(state: BotState) -> dict[str, Any]:
        return {
            "user_profile": {
                "id": user.id,
                "telegram_user_id": user.telegram_user_id,
                "telegram_username": user.telegram_username,
                "telegram_first_name": user.telegram_first_name,
                "telegram_last_name": user.telegram_last_name,
                "preferred_language": user.preferred_language,
                "patient_name": user.patient_name,
                "primary_phone": user.primary_phone,
            },
            "conversation_summary": conversation.summary,
            "tool_calls": [
                *state["tool_calls"],
                {"tool": "get_user_profile", "status": "success"},
            ],
        }

    async def classify_intent(state: BotState) -> dict[str, Any]:
        text_intent = await classify_user_intent(
            state["input_text"],
            language=state["preferred_language"],
            current_flow=conversation.current_flow,
            current_state=conversation.current_state,
        )
        if is_owner_sales_in_progress(conversation):
            intent = "owner_sales"
        else:
            intent = text_intent
        return {"intent": intent}

    async def safety_guard(state: BotState) -> dict[str, Any]:
        intent = state["intent"]
        if intent == "medical_question":
            safety_status = "medical_advice"
        elif intent == "emergency":
            safety_status = "emergency"
        elif intent in {"discount_request", "non_standard_service", "angry_user"}:
            safety_status = "needs_escalation"
        else:
            safety_status = "safe"
        return {"safety_status": safety_status}

    async def admin_faq(state: BotState) -> dict[str, Any]:
        language = state["preferred_language"]
        knowledge = await get_clinic_knowledge(session, language)
        answer = await generate_admin_faq_answer(
            question=state["input_text"],
            language=language,
            knowledge=knowledge,
            session=session,
            user=user,
            conversation=conversation,
            input_message_id=state["input_message_id"],
        )
        escalation_payload: dict[str, Any] = {}
        if not answer.answered:
            escalation_payload = await _send_to_admin(
                state=state,
                user=user,
                conversation=conversation,
                session=session,
                admin_bot=admin_bot,
                reason="unknown",
            )
        return {
            "final_response_text": answer.text,
            "faq_answered": answer.answered,
            "faq_source": answer.source,
            "tool_calls": [
                *state["tool_calls"],
                {"tool": "get_clinic_knowledge", "status": "success"},
                *escalation_payload.pop("tool_calls", []),
            ],
            **escalation_payload,
        }

    async def owner_sales(state: BotState) -> dict[str, Any]:
        language = state["preferred_language"]
        result = await handle_owner_sales_message(
            session=session,
            user=user,
            conversation=conversation,
            input_text=state["input_text"],
            language=language,
            admin_bot=admin_bot,
        )
        return {
            "final_response_text": result.text,
            "owner_sales_stage": result.stage,
            "owner_name": result.owner_name,
            "owner_clinic_name": result.clinic_name,
            "owner_locations": result.locations,
            "owner_contact": result.owner_contact,
            "owner_phone": result.phone,
            "admin_notification_sent": (
                state["admin_notification_sent"]
                or result.admin_notification_sent
            ),
            "admin_message_id": (
                result.admin_message_id or state["admin_message_id"]
            ),
            "tool_calls": [
                *state["tool_calls"],
                *result.tool_calls,
            ],
        }

    async def send_to_admin_node(state: BotState) -> dict[str, Any]:
        language = state["preferred_language"]
        reason = state["intent"] or "unknown"
        escalation_payload = await _send_to_admin(
            state=state,
            user=user,
            conversation=conversation,
            session=session,
            admin_bot=admin_bot,
            reason=reason,
        )
        has_phone = escalation_payload["escalation_phone"] is not None
        return {
            "final_response_text": _admin_handoff_text(language, has_phone=has_phone),
            **escalation_payload,
        }

    async def fallback(state: BotState) -> dict[str, Any]:
        language = state["preferred_language"]
        return {"final_response_text": text("fallback", language)}

    return {
        "load_user_context": load_user_context,
        "classify_intent": classify_intent,
        "safety_guard": safety_guard,
        "admin_faq": admin_faq,
        "owner_sales": owner_sales,
        "send_to_admin": send_to_admin_node,
        "fallback": fallback,
    }


async def _send_to_admin(
    *,
    state: BotState,
    user: User,
    conversation: Conversation,
    session: AsyncSession,
    admin_bot: Any | None,
    reason: str,
) -> dict[str, Any]:
    phone = _extract_phone(state["input_text"])
    escalation = await EscalationRepository(session).create(
        user_id=user.id,
        reason=reason,
        summary=_build_admin_summary(state),
        phone=phone,
    )
    conversation.current_flow = None
    conversation.current_state = None
    conversation.summary = None
    notification = await send_admin_notification(
        bot=admin_bot,
        message_text=_build_admin_notification_text(
            escalation_id=escalation.id,
            reason=reason,
            state=state,
            user=user,
            conversation=conversation,
            phone=phone,
        ),
    )
    if notification.admin_chat_id is not None:
        escalation.admin_chat_id = notification.admin_chat_id
    if notification.admin_message_id is not None:
        escalation.admin_message_id = notification.admin_message_id
    await session.flush()

    return {
        "should_escalate": True,
        "escalation_reason": reason,
        "escalation_id": escalation.id,
        "escalation_phone": phone,
        "missing_fields": [] if phone else ["phone"],
        "admin_notification_sent": notification.sent,
        "admin_message_id": notification.admin_message_id,
        "tool_calls": [
            {
                "tool": "send_to_admin",
                "status": "success" if notification.sent else "saved",
            },
        ],
    }


def route_intent(state: BotState) -> str:
    intent = state["intent"]
    safety_status = state["safety_status"]
    if intent == "owner_sales":
        return "owner_sales"
    if safety_status in {"emergency", "needs_escalation"}:
        return "send_to_admin"
    if intent in {
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "view_appointments",
        "unknown",
    }:
        return "send_to_admin"
    if intent in {"admin_faq", "medical_question"}:
        return "admin_faq"
    return "fallback"


def _extract_phone(text_value: str) -> str | None:
    match = re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", text_value)
    if match is None:
        return None
    return re.sub(r"[^\d+]", "", match.group(0))


def _build_admin_summary(state: BotState) -> str:
    return (
        f"Reason: {state['intent'] or 'unknown'}\n"
        f"Safety: {state['safety_status'] or 'unknown'}\n"
        f"Message: {state['input_text']}"
    )


def _build_admin_notification_text(
    *,
    escalation_id: int,
    reason: str,
    state: BotState,
    user: User,
    conversation: Conversation,
    phone: str | None,
) -> str:
    username = f"@{user.telegram_username}" if user.telegram_username else "-"
    return "\n".join(
        [
            "Admin request",
            "",
            f"Escalation ID: {escalation_id}",
            f"Reason: {reason}",
            "",
            "User:",
            f"Telegram: {username} / id {user.telegram_user_id}",
            f"Phone: {phone or '-'}",
            f"Language: {state['preferred_language']}",
            "",
            "User message:",
            state["input_text"],
            "",
            "Conversation summary:",
            conversation.summary or "-",
            "",
            f"Trace ID: {state['trace_id']}",
        ]
    )


def _admin_handoff_text(language: Language, *, has_phone: bool) -> str:
    if has_phone:
        return {
            "ru": "Передал запрос администратору. С вами свяжутся.",
            "uz": "So'rovingiz administratorga yuborildi. Siz bilan bog'lanishadi.",
            "en": "I sent your request to an administrator. They will contact you.",
        }[language]
    return {
        "ru": (
            "Передам запрос администратору. Напишите, пожалуйста, ваш номер "
            "телефона, чтобы с вами могли связаться."
        ),
        "uz": (
            "So'rovingizni administratorga yuboraman. Iltimos, bog'lanish "
            "uchun telefon raqamingizni yozing."
        ),
        "en": (
            "I will send your request to an administrator. Please send your "
            "phone number so they can contact you."
        ),
    }[language]
