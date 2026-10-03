"""
@file integrations/llm/__init__.py
@description Public surface of the LLM integration layer.

Callers import from here, never from provider.py or router.py directly, so the
internal split can change without touching domain code.
"""

from src.integrations.llm.provider import get_provider_chain
from src.integrations.llm.router import (
    LLMCallError,
    ProviderRejectedOutput,
    complete_json,
    complete_text,
    reset_breakers,
)

__all__ = [
    "get_provider_chain",
    "LLMCallError",
    "ProviderRejectedOutput",
    "complete_json",
    "complete_text",
    "reset_breakers",
]