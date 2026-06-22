import logging
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Message

logger = logging.getLogger(__name__)

PENDING_ATTACHMENTS_KEY = "pending_admin_attachments"


@dataclass(frozen=True)
class StoredAdminAttachment:
    message_id: int
    path: Path
    original_file_name: str
    mime_type: str | None
    size_bytes: int | None


def create_pending_attachment_path(
    original_file_name: str,
    *,
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    storage_dir = Path(resolved.user_attachment_storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_file_name(original_file_name)
    return storage_dir / f"{uuid4().hex}-{safe_name}"


def add_pending_attachments_to_message(
    message: Message,
    attachments: list[StoredAdminAttachment],
) -> None:
    payload = dict(message.raw_payload or {})
    current = payload.get(PENDING_ATTACHMENTS_KEY)
    records = list(current) if isinstance(current, list) else []
    for attachment in attachments:
        records.append(
            {
                "path": str(attachment.path),
                "original_file_name": attachment.original_file_name,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
            }
        )
    payload[PENDING_ATTACHMENTS_KEY] = records
    message.raw_payload = payload


async def get_pending_admin_attachments(
    session: AsyncSession,
    *,
    user_id: int,
    conversation_id: int | None,
) -> list[StoredAdminAttachment]:
    if conversation_id is None:
        return []
    result = await session.execute(
        select(Message)
        .where(
            Message.user_id == user_id,
            Message.conversation_id == conversation_id,
            Message.direction == "in",
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    attachments: list[StoredAdminAttachment] = []
    for message in result.scalars():
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        raw_attachments = payload.get(PENDING_ATTACHMENTS_KEY)
        if not isinstance(raw_attachments, list):
            continue
        for raw in raw_attachments:
            if not isinstance(raw, dict):
                continue
            path_text = str(raw.get("path") or "").strip()
            if not path_text:
                continue
            attachments.append(
                StoredAdminAttachment(
                    message_id=message.id,
                    path=Path(path_text),
                    original_file_name=str(
                        raw.get("original_file_name") or Path(path_text).name
                    ),
                    mime_type=(
                        str(raw["mime_type"])
                        if isinstance(raw.get("mime_type"), str)
                        else None
                    ),
                    size_bytes=(
                        int(raw["size_bytes"])
                        if isinstance(raw.get("size_bytes"), int)
                        else None
                    ),
                )
            )
    return attachments


async def clear_pending_admin_attachments(
    session: AsyncSession,
    attachments: list[StoredAdminAttachment],
) -> None:
    if not attachments:
        return
    message_ids = {attachment.message_id for attachment in attachments}
    result = await session.execute(select(Message).where(Message.id.in_(message_ids)))
    for message in result.scalars():
        payload = dict(message.raw_payload or {})
        payload.pop(PENDING_ATTACHMENTS_KEY, None)
        payload["admin_attachments_forwarded"] = True
        message.raw_payload = payload
    await session.flush()


def delete_attachment_files(attachments: list[StoredAdminAttachment]) -> None:
    for attachment in attachments:
        try:
            if attachment.path.exists():
                attachment.path.unlink()
                logger.info(
                    "pending_admin_attachment_deleted",
                    extra={"path": str(attachment.path)},
                )
        except OSError:
            logger.exception(
                "pending_admin_attachment_delete_failed",
                extra={"path": str(attachment.path)},
            )


def _safe_file_name(value: str) -> str:
    name = Path(value or "document").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:120] or "document"
