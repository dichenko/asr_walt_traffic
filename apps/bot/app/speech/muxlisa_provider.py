import base64
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import Settings
from app.speech.base import SpeechProviderError, TextToSpeechResult
from app.speech.temp_files import create_temp_audio_path

logger = logging.getLogger(__name__)

MUXLISA_TTS_MODEL = "muxlisa-tts"


class MuxlisaSpeechProvider:
    _config_logged = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def synthesize(
        self, text: str, language: str, instructions: str | None = None
    ) -> TextToSpeechResult:
        self._log_config_once()
        prepared_text = _prepare_text_for_tts(text)
        if not prepared_text:
            raise SpeechProviderError("Muxlisa TTS input is empty")
        if len(prepared_text) > self.settings.muxlisa_tts_max_chars:
            max_chars = self.settings.muxlisa_tts_max_chars
            truncated_text = prepared_text[:max_chars]
            prepared_text = truncated_text.rsplit(" ", 1)[0] or truncated_text

        started_at = time.perf_counter()
        audio_bytes = await self._synthesize_once(prepared_text)
        output_path = create_temp_audio_path(suffix=".ogg")
        output_path.write_bytes(audio_bytes)
        logger.info(
            "speech_provider_call_succeeded",
            extra={
                "provider": "muxlisa",
                "operation": "tts",
                "model": MUXLISA_TTS_MODEL,
                "voice": str(self.settings.muxlisa_tts_speaker),
                "language": language,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "file_size_bytes": output_path.stat().st_size,
            },
        )
        return TextToSpeechResult(
            file_path=str(output_path),
            mime_type="audio/ogg",
            format="opus",
            provider="muxlisa",
            model=MUXLISA_TTS_MODEL,
            voice=str(self.settings.muxlisa_tts_speaker),
        )

    async def _synthesize_once(self, text: str) -> bytes:
        headers = {
            "Authorization": f"Bearer {self._api_key_or_raise()}",
            "Accept": "application/json, audio/ogg, audio/wav, audio/mpeg",
        }
        payload = {
            "text": text,
            "speaker": str(self.settings.muxlisa_tts_speaker),
        }
        timeout = self.settings.muxlisa_tts_timeout_ms / 1000
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url()}/tts",
                headers=headers,
                data=payload,
            )
            if response.status_code >= 400:
                raise self._muxlisa_error(response)
            return await self._audio_bytes_from_response(client, response, headers)

    async def _audio_bytes_from_response(
        self,
        client: httpx.AsyncClient,
        response: httpx.Response,
        headers: dict[str, str],
    ) -> bytes:
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("audio/"):
            return response.content

        try:
            payload = response.json()
        except ValueError as exc:
            raise SpeechProviderError("Muxlisa TTS response is not valid JSON") from exc

        audio_base64 = _first_str(payload, "audio", "audio_base64", "data")
        if audio_base64:
            try:
                return base64.b64decode(audio_base64)
            except ValueError as exc:
                raise SpeechProviderError("Muxlisa TTS audio is not valid base64") from exc

        audio_path = _first_str(payload, "audio_path", "audio_url", "url", "file")
        if audio_path:
            audio_response = await client.get(
                urljoin(f"{self._base_url()}/", audio_path),
                headers=headers,
            )
            if audio_response.status_code >= 400:
                raise self._muxlisa_error(audio_response)
            return audio_response.content

        raise SpeechProviderError("Muxlisa TTS response did not include audio")

    def _api_key_or_raise(self) -> str:
        if self.settings.muxlisa_api_key is None:
            raise SpeechProviderError("MUXLISA_API_KEY is required for Muxlisa TTS")
        api_key = self.settings.muxlisa_api_key.get_secret_value().strip()
        if not api_key:
            raise SpeechProviderError("MUXLISA_API_KEY is required for Muxlisa TTS")
        return api_key

    def _base_url(self) -> str:
        return self.settings.muxlisa_base_url.rstrip("/")

    def _log_config_once(self) -> None:
        if MuxlisaSpeechProvider._config_logged:
            return
        MuxlisaSpeechProvider._config_logged = True
        logger.info(
            "muxlisa_tts_config",
            extra={
                "base_url": self._base_url(),
                "speaker": self.settings.muxlisa_tts_speaker,
                "max_chars": self.settings.muxlisa_tts_max_chars,
            },
        )

    def _muxlisa_error(self, response: httpx.Response) -> "MuxlisaStatusError":
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
        else:
            body = response.text
        logger.error(
            "muxlisa_tts_failed",
            extra={
                "status_code": response.status_code,
                "body": str(body)[:1000],
                "speaker": self.settings.muxlisa_tts_speaker,
            },
        )
        return MuxlisaStatusError(
            f"Muxlisa TTS failed: status={response.status_code}, body={body}",
            response.status_code,
        )


class MuxlisaStatusError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
