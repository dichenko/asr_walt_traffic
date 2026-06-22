import asyncio
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}

USER_NO_TEXT_MESSAGE = (
    "Не удалось распознать текст из присланных файлов. Пришлите, пожалуйста, "
    "PDF или чёткие фотографии страниц меню в JPG, PNG или WEBP."
)


@dataclass(frozen=True)
class IncomingAttachment:
    file_id: str
    original_file_name: str
    declared_mime_type: str | None
    size_bytes: int | None
    local_path: Path


@dataclass(frozen=True)
class OcrPageResult:
    page_index: int
    markdown: str


@dataclass(frozen=True)
class OcrDocumentResult:
    original_file_name: str
    mime_type: str
    page_count: int
    pages: list[OcrPageResult]


@dataclass(frozen=True)
class OcrFileFailure:
    original_file_name: str
    reason: str
    user_message: str


@dataclass(frozen=True)
class OcrBatchResult:
    documents: list[OcrDocumentResult]
    failures: list[OcrFileFailure]

    @property
    def has_text(self) -> bool:
        return any(
            page.markdown.strip()
            for document in self.documents
            for page in document.pages
        )

    @property
    def status(self) -> Literal["success", "partial_success", "failed"]:
        if self.documents and self.failures:
            return "partial_success"
        if self.documents:
            return "success"
        return "failed"


class MistralOcrService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def recognize_documents(
        self,
        files: list[IncomingAttachment],
        *,
        trace_id: str | None = None,
        telegram_user_id: int | None = None,
        telegram_chat_id: int | None = None,
    ) -> OcrBatchResult:
        if len(files) > self.settings.menu_import_max_files:
            return OcrBatchResult(
                documents=[],
                failures=[
                    OcrFileFailure(
                        original_file_name="",
                        reason="too_many_files",
                        user_message=(
                            "Слишком много файлов для распознавания. "
                            f"Пришлите до {self.settings.menu_import_max_files} файлов."
                        ),
                    )
                ],
            )

        total_size = sum(_path_size(file.local_path, file.size_bytes) for file in files)
        max_total = self.settings.menu_import_max_total_size_mb * 1024 * 1024
        if total_size > max_total:
            return OcrBatchResult(
                documents=[],
                failures=[
                    OcrFileFailure(
                        original_file_name="",
                        reason="total_size_limit",
                        user_message=(
                            "Файлы слишком большие для распознавания. "
                            f"Пришлите суммарно до "
                            f"{self.settings.menu_import_max_total_size_mb} МБ."
                        ),
                    )
                ],
            )

        documents: list[OcrDocumentResult] = []
        failures: list[OcrFileFailure] = []
        for file in files:
            result = await self._recognize_one(
                file,
                trace_id=trace_id,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
            )
            if isinstance(result, OcrDocumentResult):
                documents.append(result)
            else:
                failures.append(result)

        return OcrBatchResult(documents=documents, failures=failures)

    async def _recognize_one(
        self,
        file: IncomingAttachment,
        *,
        trace_id: str | None,
        telegram_user_id: int | None,
        telegram_chat_id: int | None,
    ) -> OcrDocumentResult | OcrFileFailure:
        started = perf_counter()
        detected_mime = detect_supported_mime_type(
            file.local_path,
            declared_mime_type=file.declared_mime_type,
        )
        size_bytes = _path_size(file.local_path, file.size_bytes)
        max_file_size = self.settings.menu_import_max_file_size_mb * 1024 * 1024
        validation_result = "success"

        if size_bytes > max_file_size:
            validation_result = "file_size_limit"
            failure = OcrFileFailure(
                original_file_name=file.original_file_name,
                reason=validation_result,
                user_message=(
                    "Файл слишком большой для распознавания. "
                    f"Пришлите файл до "
                    f"{self.settings.menu_import_max_file_size_mb} МБ."
                ),
            )
            self._log_file_result(file, detected_mime, size_bytes, validation_result, 0)
            return failure

        if detected_mime is None:
            validation_result = "unsupported_format"
            failure = OcrFileFailure(
                original_file_name=file.original_file_name,
                reason=validation_result,
                user_message=(
                    "Этот формат пока не поддерживается. Пришлите PDF или "
                    "изображение JPG, PNG либо WEBP."
                ),
            )
            self._log_file_result(file, None, size_bytes, validation_result, 0)
            return failure

        api_key = (
            self.settings.mistral_api_key.get_secret_value()
            if self.settings.mistral_api_key is not None
            else None
        )
        if not api_key:
            logger.error(
                "mistral_ocr_api_key_missing",
                extra={"trace_id": trace_id, "telegram_user_id": telegram_user_id},
            )
            return OcrFileFailure(
                original_file_name=file.original_file_name,
                reason="missing_api_key",
                user_message=(
                    "Не удалось распознать документ. Попробуйте отправить более "
                    "чёткий файл или повторите попытку позже."
                ),
            )

        try:
            payload = await asyncio.to_thread(
                _build_ocr_payload,
                file.local_path,
                detected_mime,
                self.settings.mistral_ocr_model,
            )
            response_payload = await self._post_ocr(payload, api_key=api_key)
            pages = _parse_ocr_pages(response_payload)
            text_chars = sum(len(page.markdown) for page in pages)
            if not any(page.markdown.strip() for page in pages):
                return OcrFileFailure(
                    original_file_name=file.original_file_name,
                    reason="empty_ocr_text",
                    user_message=(
                        "В документе не удалось найти читаемый текст. Пришлите "
                        "более чёткую фотографию или другой PDF."
                    ),
                )

            duration_ms = int((perf_counter() - started) * 1000)
            logger.info(
                "mistral_ocr_file_processed",
                extra={
                    "trace_id": trace_id,
                    "telegram_user_id": telegram_user_id,
                    "telegram_chat_id": telegram_chat_id,
                    "file_name": file.original_file_name,
                    "mime_type": detected_mime,
                    "size_bytes": size_bytes,
                    "validation_result": validation_result,
                    "ocr_duration_ms": duration_ms,
                    "page_count": len(pages),
                    "ocr_chars": text_chars,
                    "status": "success",
                },
            )
            return OcrDocumentResult(
                original_file_name=file.original_file_name,
                mime_type=detected_mime,
                page_count=len(pages),
                pages=pages,
            )
        except Exception as exc:
            duration_ms = int((perf_counter() - started) * 1000)
            logger.warning(
                "mistral_ocr_file_failed",
                extra={
                    "trace_id": trace_id,
                    "telegram_user_id": telegram_user_id,
                    "telegram_chat_id": telegram_chat_id,
                    "file_name": file.original_file_name,
                    "mime_type": detected_mime,
                    "size_bytes": size_bytes,
                    "validation_result": validation_result,
                    "ocr_duration_ms": duration_ms,
                    "error": _safe_error_text(exc),
                    "status": "failed",
                },
            )
            return OcrFileFailure(
                original_file_name=file.original_file_name,
                reason="mistral_error",
                user_message=(
                    "Не удалось распознать документ. Попробуйте отправить более "
                    "чёткий файл или повторите попытку позже."
                ),
            )

    async def _post_ocr(
        self,
        payload: dict[str, Any],
        *,
        api_key: str,
    ) -> dict[str, Any]:
        endpoint = f"{self.settings.mistral_base_url.rstrip('/')}/v1/ocr"
        timeout = httpx.Timeout(self.settings.mistral_timeout_ms / 1000)
        headers = {"Authorization": f"Bearer {api_key}"}
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    if response.status_code < 400:
                        return response.json()
                    if response.status_code not in (429, 500, 502, 503, 504):
                        raise RuntimeError(
                            f"Mistral OCR failed with HTTP {response.status_code}: "
                            f"{response.text[:300]}"
                        )
                    last_error = RuntimeError(
                        f"Mistral OCR temporary HTTP {response.status_code}: "
                        f"{response.text[:300]}"
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc

                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))

        if last_error is not None:
            raise last_error
        raise RuntimeError("Mistral OCR failed")

    def _log_file_result(
        self,
        file: IncomingAttachment,
        detected_mime: str | None,
        size_bytes: int,
        validation_result: str,
        ocr_duration_ms: int,
    ) -> None:
        logger.info(
            "mistral_ocr_file_rejected",
            extra={
                "file_name": file.original_file_name,
                "mime_type": detected_mime or file.declared_mime_type,
                "size_bytes": size_bytes,
                "validation_result": validation_result,
                "ocr_duration_ms": ocr_duration_ms,
                "status": "failed",
            },
        )


def detect_supported_mime_type(
    path: Path,
    *,
    declared_mime_type: str | None = None,
) -> str | None:
    with path.open("rb") as file:
        head = file.read(16)

    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if declared_mime_type in SUPPORTED_MIME_TYPES:
        return declared_mime_type
    return None


def _build_ocr_payload(path: Path, mime_type: str, model: str) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "model": model,
        "document": {
            "type": "document_url",
            "document_url": f"data:{mime_type};base64,{encoded}",
        },
    }


def _parse_ocr_pages(payload: dict[str, Any]) -> list[OcrPageResult]:
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return []

    parsed: list[OcrPageResult] = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        markdown = page.get("markdown") or page.get("text") or ""
        page_index = page.get("index", index)
        if not isinstance(page_index, int):
            page_index = index
        parsed.append(OcrPageResult(page_index=page_index, markdown=str(markdown)))
    return parsed


def _path_size(path: Path, fallback: int | None) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return fallback or 0


def _safe_error_text(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]
