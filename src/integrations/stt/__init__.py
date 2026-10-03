"""
@file integrations/stt/__init__.py
@description Public surface of the speech-to-text integration layer.
"""

from src.integrations.stt.router import (
    TranscriptionError,
    TranscriptionResult,
    transcribe_audio,
)

__all__ = ["TranscriptionError", "TranscriptionResult", "transcribe_audio"]