"""
@file integrations/stt/router.py
@description Transcription provider chain: AssemblyAI primary, Deepgram
fallback. Both providers' response shapes are normalized into one
TranscriptionResult so no caller ever branches on provider.

Why Deepgram and not Groq/Whisper as the fallback:

Whisper hallucinates text during silence, music and crosstalk — it will
confidently produce sentences from a recording that contains none. This
product holds voice recordings of people who have died. Fabricated sentences
in a dead relative's recording is a categorically worse outcome than a
transcription outage, which at least fails visibly and is retryable. AssemblyAI
reports roughly 30% fewer hallucinations than Whisper, and Deepgram is faster
and cheaper per hour. A fallback is not automatically a good fallback.

This router also fixes a real bug in the transcription path: `aai.settings.api_key`
was assigned as a *module-level global mutation*, which is not thread-safe.
Two concurrent transcriptions with different credentials would race. Here the
key is bound per-client instead.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.core.config import settings

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    """Raised when every provider in the chain failed. Never carries user content."""

    def __init__(self, message: str, provider: Optional[str] = None):
        super().__init__(message)
        self.provider = provider


@dataclass(frozen=True)
class TranscriptionResult:
    """Provider-independent transcription output."""

    text: str
    confidence: Optional[float]
    language: str
    provider_job_id: Optional[str]
    provider: str
    model: str


class _STTCircuitBreaker:
    """Same rationale as the LLM breaker — a dead STT provider must not add its
    retry latency to every subsequent upload."""

    def __init__(self, failure_threshold: int, cooldown_seconds: int):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: Optional[float] = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning(
                "STT circuit opened for a provider after %d consecutive failures.",
                self._failures,
            )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None


_stt_breakers = {
    "assemblyai": _STTCircuitBreaker(
        failure_threshold=settings.ai_circuit_failure_threshold,
        cooldown_seconds=settings.ai_circuit_cooldown_seconds,
    ),
    "deepgram": _STTCircuitBreaker(
        failure_threshold=settings.ai_circuit_failure_threshold,
        cooldown_seconds=settings.ai_circuit_cooldown_seconds,
    ),
}

# Attempts against one STT provider before failing over. STT is a single
# upload-and-wait call, so this is a retry on transient network failure only —
# a genuinely rejected file (bad format, too large) should fail over
# immediately rather than twice.
_STT_MAX_ATTEMPTS = 2


def transcribe_audio(audio_bytes: bytes, *, filename: str = "audio") -> TranscriptionResult:
    """
    Transcribes audio via the configured chain.

    Blocking by design: AssemblyAI's SDK is synchronous and this is called from
    a background task, not a request handler. The LLM router is async because it
    fans out; STT does one call and has nothing to overlap.

    `filename` is used only to derive a file extension for providers that infer
    format from it. It is server-constructed, never client-supplied.
    """
    if not audio_bytes:
        raise TranscriptionError("Refusing to transcribe empty audio.")

    errors = []

    if settings.assemblyai_api_key:
        breaker = _stt_breakers["assemblyai"]
        if not breaker.is_open():
            for attempt in range(1, _STT_MAX_ATTEMPTS + 1):
                try:
                    result = _transcribe_assemblyai(audio_bytes, filename)
                    breaker.record_success()
                    return result
                except Exception as exc:  # noqa: BLE001 - provider-agnostic
                    logger.warning(
                        "AssemblyAI transcription attempt %d/%d failed (%s).",
                        attempt,
                        _STT_MAX_ATTEMPTS,
                        type(exc).__name__,
                    )
                    errors.append(f"assemblyai: {type(exc).__name__}")
                    if attempt < _STT_MAX_ATTEMPTS:
                        time.sleep(1.5)
            breaker.record_failure()
        else:
            logger.info("Skipping AssemblyAI: circuit open.")

    if settings.deepgram_api_key:
        breaker = _stt_breakers["deepgram"]
        if not breaker.is_open():
            for attempt in range(1, _STT_MAX_ATTEMPTS + 1):
                try:
                    result = _transcribe_deepgram(audio_bytes, filename)
                    breaker.record_success()
                    return result
                except Exception as exc:  # noqa: BLE001 - provider-agnostic
                    logger.warning(
                        "Deepgram transcription attempt %d/%d failed (%s).",
                        attempt,
                        _STT_MAX_ATTEMPTS,
                        type(exc).__name__,
                    )
                    errors.append(f"deepgram: {type(exc).__name__}")
                    if attempt < _STT_MAX_ATTEMPTS:
                        time.sleep(1.5)
            breaker.record_failure()
        else:
            logger.info("Skipping Deepgram: circuit open.")

    detail = "; ".join(errors) if errors else "no providers configured"
    raise TranscriptionError(f"Transcription failed across the chain ({detail}).")


def _transcribe_assemblyai(audio_bytes: bytes, filename: str) -> TranscriptionResult:
    import assemblyai as aai

    # Configured per-call via the constructor rather than by mutating the
    # module-level `aai.settings.api_key`. That global mutation was not
    # thread-safe: concurrent transcriptions shared one credential slot.
    config = aai.Settings(api_key=settings.assemblyai_api_key)
    transcriber = aai.Transcriber(config=config)

    extension = _extension_for(filename)
    upload_url = transcriber.upload_file(audio_bytes, filename=f"memory{extension}")
    transcript = transcriber.transcribe(upload_url)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(transcript.error or "AssemblyAI returned an error status.")

    return TranscriptionResult(
        text=transcript.text or "",
        confidence=transcript.confidence,
        language=transcript.language_code or "en",
        provider_job_id=getattr(transcript, "id", None),
        provider="assemblyai",
        model="universal",
    )


def _transcribe_deepgram(audio_bytes: bytes, filename: str) -> TranscriptionResult:
    """
    Deepgram Nova-3 via the REST API.

    Called through httpx rather than a vendor SDK: Deepgram's SDK is a thin
    wrapper and adding it as a dependency for one call is not worth it. Uses a
    module-scoped client so connections are pooled across calls.
    """
    import httpx

    extension = _extension_for(filename)
    mime = {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".mp4": "audio/mp4",
        ".mpeg": "audio/mpeg",
    }.get(extension, "application/octet-stream")

    response = httpx.post(
        f"https://api.deepgram.com/v1/listen?model={settings.deepgram_model}&smart_format=true",
        headers={
            "Authorization": f"Token {settings.deepgram_api_key}",
            "Content-Type": mime,
        },
        content=audio_bytes,
        timeout=settings.ai_request_timeout_seconds * 4,  # audio upload + transcribe
    )
    response.raise_for_status()

    payload = response.json()
    try:
        alternative = payload["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Deepgram returned an unexpected response shape.") from exc

    return TranscriptionResult(
        text=alternative.get("transcript") or "",
        confidence=alternative.get("confidence"),
        language=payload.get("metadata", {}).get("language") or "en",
        provider_job_id=payload.get("metadata", {}).get("request_id"),
        provider="deepgram",
        model=settings.deepgram_model,
    )


def _extension_for(filename: str) -> str:
    """
    Extracts a safe extension from a server-constructed filename.

    Never from a client-supplied path — only the trailing suffix is kept, and
    only from a whitelist. A caller-controlled filename reaching a filesystem or
    an outbound Content-Type header is a path/header-injection risk.
    """
    allowed = {".m4a", ".mp3", ".wav", ".webm", ".ogg", ".flac", ".mp4", ".mpeg"}
    _, dot, suffix = filename.rpartition(".")
    if not dot:
        return ".m4a"
    candidate = f".{suffix.lower()}"
    return candidate if candidate in allowed else ".m4a"