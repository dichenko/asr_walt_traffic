import logging
import re
import time
from typing import Any

import httpx

from app.config import Settings
from app.speech.base import SpeechProviderError, TextToSpeechResult
from app.speech.temp_files import create_temp_audio_path

logger = logging.getLogger(__name__)

HUME_TTS_MODEL = "hume-tts-v2"


class HumeSpeechProvider:
    _config_logged = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def synthesize(
        self, text: str, language: str, instructions: str | None = None
    ) -> TextToSpeechResult:
        self._log_config_once()
        prepared_text = _prepare_text_for_tts(text)
        if not prepared_text:
            raise SpeechProviderError("Hume TTS input is empty")
        if len(prepared_text) > self.settings.hume_tts_max_chars:
            max_chars = self.settings.hume_tts_max_chars
            truncated_text = prepared_text[:max_chars]
            prepared_text = truncated_text.rsplit(" ", 1)[0] or truncated_text

        started_at = time.perf_counter()
        audio_bytes = await self._synthesize_once(prepared_text)
        output_path = create_temp_audio_path(suffix=".mp3")
        output_path.write_bytes(audio_bytes)
        logger.info(
            "speech_provider_call_succeeded",
            extra={
                "provider": "hume",
                "operation": "tts",
                "model": HUME_TTS_MODEL,
                "voice": self.settings.hume_voice_id,
                "language": language,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "file_size_bytes": output_path.stat().st_size,
            },
        )
        return TextToSpeechResult(
            file_path=str(output_path),
            mime_type="audio/mpeg",
            format="mp3",
            provider="hume",
            model=HUME_TTS_MODEL,
            voice=self.settings.hume_voice_id,
        )

    async def _synthesize_once(self, text: str) -> bytes:
        headers = {
            "X-Hume-Api-Key": self._api_key_or_raise(),
            "Content-Type": "application/json",
            "Accept": "audio/mpeg, audio/mp3, application/octet-stream",
        }
        payload = {
            "version": "2",
            "utterances": [
                {
                    "text": text,
                    "voice": {"id": self._voice_id_or_raise()},
                    "speed": self.settings.hume_tts_speed,
                }
            ],
            "format": {"type": "mp3"},
            "num_generations": 1,
            "split_utterances": False,
        }
        timeout = self.settings.hume_tts_timeout_ms / 1000
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url()}/v0/tts/file",
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            raise self._hume_error(response)
        if not response.content:
            raise SpeechProviderError("Hume TTS response did not include audio")
        return response.content

    def _api_key_or_raise(self) -> str:
        if self.settings.hume_api_key is None:
            raise SpeechProviderError("HUME_API_KEY is required for Hume TTS")
        api_key = self.settings.hume_api_key.get_secret_value().strip()
        if not api_key:
            raise SpeechProviderError("HUME_API_KEY is required for Hume TTS")
        return api_key

    def _voice_id_or_raise(self) -> str:
        voice_id = self.settings.hume_voice_id.strip()
        if not voice_id:
            raise SpeechProviderError("HUME_VOICE_ID is required for Hume TTS")
        return voice_id

    def _base_url(self) -> str:
        return self.settings.hume_tts_base_url.rstrip("/")

    def _log_config_once(self) -> None:
        if HumeSpeechProvider._config_logged:
            return
        HumeSpeechProvider._config_logged = True
        logger.info(
            "hume_tts_config",
            extra={
                "base_url": self._base_url(),
                "voice": self.settings.hume_voice_id,
                "speed": self.settings.hume_tts_speed,
                "max_chars": self.settings.hume_tts_max_chars,
            },
        )

    def _hume_error(self, response: httpx.Response) -> "HumeStatusError":
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
        else:
            body = response.text
        logger.error(
            "hume_tts_failed",
            extra={
                "status_code": response.status_code,
                "body": str(body)[:1000],
                "voice": self.settings.hume_voice_id,
                "speed": self.settings.hume_tts_speed,
            },
        )
        return HumeStatusError(
            f"Hume TTS failed: status={response.status_code}, body={body}",
            response.status_code,
        )


class HumeStatusError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _prepare_text_for_tts(text: str) -> str:
    prepared = text.strip()
    prepared = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", prepared)
    prepared = re.sub(r"https?://\S+", "", prepared)
    prepared = re.sub(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]", "", prepared)
    prepared = prepared.replace("**", "")
    prepared = prepared.replace("__", "")
    prepared = prepared.replace("`", "")
    prepared = prepared.replace("\u2022", ". ")
    prepared = prepared.replace("-", " ")
    prepared = re.sub(r"\s+", " ", prepared)
    return prepared.strip()
