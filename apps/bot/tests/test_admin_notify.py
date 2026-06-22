from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app.config import Settings
from app.services.admin_notify import (
    AdminDocumentToSend,
    send_admin_documents,
    send_admin_notification,
)


class FailingBot:
    async def send_message(self, *, chat_id: str, text: str):
        raise TelegramBadRequest(
            method=SendMessage(chat_id=chat_id, text=text),
            message="chat not found",
        )


async def test_send_admin_notification_does_not_raise_on_telegram_error():
    result = await send_admin_notification(
        bot=FailingBot(),
        message_text="Escalation required",
        settings=Settings(admin_telegram_chat_id="-100bad"),
    )

    assert result.sent is False
    assert result.admin_chat_id == "-100bad"
    assert result.admin_message_id is None


async def test_send_admin_documents_sends_each_file(tmp_path: Path):
    file_path = tmp_path / "menu.pdf"
    file_path.write_bytes(b"%PDF-1.7")

    class FakeMessage:
        message_id = 321

    class FakeBot:
        def __init__(self) -> None:
            self.documents = []

        async def send_document(self, **kwargs):
            self.documents.append(kwargs)
            return FakeMessage()

    bot = FakeBot()

    result = await send_admin_documents(
        bot=bot,
        documents=[
            AdminDocumentToSend(
                path=file_path,
                file_name="menu.pdf",
                caption="Escalation ID: 1",
            )
        ],
        settings=Settings(admin_telegram_chat_id="-100123"),
    )

    assert result.sent_count == 1
    assert result.failed_count == 0
    assert result.admin_message_ids == [321]
    assert bot.documents[0]["chat_id"] == "-100123"
    assert bot.documents[0]["caption"] == "Escalation ID: 1"
