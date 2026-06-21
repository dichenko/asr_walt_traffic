from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.telegram.texts import LANGUAGE_LABELS


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"language:{language}",
                )
            ]
            for language, label in LANGUAGE_LABELS.items()
        ]
    )


def contact_request_keyboard(language: str | None = None) -> ReplyKeyboardMarkup:
    labels = {
        "ru": "Отправить контакт",
        "uz": "Kontakt yuborish",
        "en": "Share contact",
    }
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=labels.get(language or "ru", labels["ru"]),
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
