from pathlib import Path

from app.config import Settings
from app.services.menu_ocr_prompt_builder import (
    TRUNCATION_NOTICE,
    build_menu_ocr_message,
)
from app.services.mistral_ocr_service import (
    OcrDocumentResult,
    OcrPageResult,
    detect_supported_mime_type,
)


def test_detect_supported_mime_type_uses_file_signature(tmp_path: Path):
    pdf = tmp_path / "menu.bin"
    pdf.write_bytes(b"%PDF-1.7\ncontent")

    png = tmp_path / "menu.txt"
    png.write_bytes(b"\x89PNG\r\n\x1a\ncontent")

    assert detect_supported_mime_type(pdf, declared_mime_type="text/plain") == (
        "application/pdf"
    )
    assert detect_supported_mime_type(png) == "image/png"


def test_detect_supported_mime_type_rejects_unknown_file(tmp_path: Path):
    unknown = tmp_path / "menu.docx"
    unknown.write_bytes(b"not a supported document")

    assert detect_supported_mime_type(unknown) is None


def test_build_menu_ocr_message_formats_pages():
    settings = Settings(menu_import_max_ocr_chars=10_000)
    message = build_menu_ocr_message(
        [
            OcrDocumentResult(
                original_file_name="menu.pdf",
                mime_type="application/pdf",
                page_count=2,
                pages=[
                    OcrPageResult(page_index=0, markdown="Борщ\nСвекла, капуста"),
                    OcrPageResult(page_index=1, markdown="Компот"),
                ],
            )
        ],
        settings=settings,
    )

    assert "Пользователь прислал меню ресторана" in message
    assert "--- Файл: menu.pdf ---" in message
    assert "[Страница 1]\nБорщ" in message
    assert "[Страница 2]\nКомпот" in message


def test_build_menu_ocr_message_adds_truncation_notice():
    settings = Settings(menu_import_max_ocr_chars=260)
    message = build_menu_ocr_message(
        [
            OcrDocumentResult(
                original_file_name="long-menu.pdf",
                mime_type="application/pdf",
                page_count=1,
                pages=[OcrPageResult(page_index=0, markdown="А" * 500)],
            )
        ],
        settings=settings,
    )

    assert len(message) <= settings.menu_import_max_ocr_chars
    assert message.endswith(TRUNCATION_NOTICE.strip())
