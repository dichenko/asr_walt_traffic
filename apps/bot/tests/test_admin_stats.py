from datetime import UTC, date, datetime, timedelta

from app.admin.stats_repository import AdminStatsRepository
from app.db.repositories import ConversationRepository, MessageRepository, UserRepository


async def test_admin_stats_counts_totals_and_daily_user_activity(session):
    users = UserRepository(session)
    conversations = ConversationRepository(session)
    messages = MessageRepository(session)
    today = date(2026, 6, 21)

    user_old = await users.upsert_from_telegram(telegram_user_id=9101)
    user_new = await users.upsert_from_telegram(telegram_user_id=9102)
    user_outgoing_only = await users.upsert_from_telegram(telegram_user_id=9103)

    old_conversation = await conversations.get_or_create(
        user_id=user_old.id,
        telegram_chat_id=9101,
    )
    new_conversation = await conversations.get_or_create(
        user_id=user_new.id,
        telegram_chat_id=9102,
    )
    outgoing_conversation = await conversations.get_or_create(
        user_id=user_outgoing_only.id,
        telegram_chat_id=9103,
    )

    old_first = await messages.save_message(
        user_id=user_old.id,
        conversation_id=old_conversation.id,
        direction="in",
        message_type="text",
        text="old first",
    )
    old_first.created_at = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

    old_active = await messages.save_message(
        user_id=user_old.id,
        conversation_id=old_conversation.id,
        direction="in",
        message_type="text",
        text="active in window",
    )
    old_active.created_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)

    new_first = await messages.save_message(
        user_id=user_new.id,
        conversation_id=new_conversation.id,
        direction="in",
        message_type="text",
        text="new first",
    )
    new_first.created_at = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)

    new_second_same_day = await messages.save_message(
        user_id=user_new.id,
        conversation_id=new_conversation.id,
        direction="in",
        message_type="text",
        text="new second",
    )
    new_second_same_day.created_at = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

    outgoing = await messages.save_message(
        user_id=user_outgoing_only.id,
        conversation_id=outgoing_conversation.id,
        direction="out",
        message_type="text",
        text="assistant only",
    )
    outgoing.created_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)

    await session.flush()

    stats = await AdminStatsRepository(session).fetch(today=today)
    by_date = {point.date: point for point in stats.daily}

    assert stats.total_users == 3
    assert stats.total_messages == 5
    assert len(stats.daily) == 30
    assert stats.daily[0].date == (today - timedelta(days=29)).isoformat()
    assert stats.daily[-1].date == today.isoformat()
    assert by_date["2026-06-20"].new_users == 1
    assert by_date["2026-06-20"].active_users == 2
    assert by_date["2026-06-21"].new_users == 0
    assert by_date["2026-06-21"].active_users == 0
