from pathlib import Path

from app.db.repositories import (
    ConversationRepository,
    MessageRepository,
    UserRepository,
)
from app.services.pending_admin_attachments import (
    StoredAdminAttachment,
    add_pending_attachments_to_message,
    clear_pending_admin_attachments,
    delete_attachment_files,
    get_pending_admin_attachments,
)


async def test_pending_admin_attachments_are_cleared_and_deleted(
    session,
    tmp_path: Path,
):
    user = await UserRepository(session).upsert_from_telegram(telegram_user_id=777)
    conversation = await ConversationRepository(session).get_or_create(
        user_id=user.id,
        telegram_chat_id=777,
    )
    message = await MessageRepository(session).save_message(
        user_id=user.id,
        conversation_id=conversation.id,
        telegram_message_id=10,
        direction="in",
        message_type="system",
        text="document",
        raw_payload={},
    )
    file_path = tmp_path / "menu.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    add_pending_attachments_to_message(
        message,
        [
            StoredAdminAttachment(
                message_id=message.id,
                path=file_path,
                original_file_name="menu.pdf",
                mime_type="application/pdf",
                size_bytes=file_path.stat().st_size,
            )
        ],
    )
    await session.flush()

    pending = await get_pending_admin_attachments(
        session,
        user_id=user.id,
        conversation_id=conversation.id,
    )

    assert len(pending) == 1
    assert pending[0].path == file_path
    assert pending[0].original_file_name == "menu.pdf"

    await clear_pending_admin_attachments(session, pending)
    delete_attachment_files(pending)

    assert not file_path.exists()
    assert await get_pending_admin_attachments(
        session,
        user_id=user.id,
        conversation_id=conversation.id,
    ) == []
