"""
@file integrations/llm/provider.py
@description One place that knows which LLM providers exist and how to reach
them. No other module in the codebase names Groq or Gemini — adding a provider
means editing this file and the router, never the domain layer.

Design notes worth keeping:

- Provider *capabilities* are declared per provider, never sniffed from the
  base URL string. The reference implementation decided its request shape with
  `if "groq" in base_url or "openai" in base_url`, which silently breaks the
  moment anyone points LLM_BASE_URL at a self-hosted vLLM or a proxy.
- `strict_structured_output` exists because Groq's Structured Outputs support
  two modes with materially different schema requirements (see
  schemas/organization.py for the strict-mode variants). Providers that only
  do best-effort JSON mode get strict=False and are validated harder downstream.
"""

from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from src.core.config import settings


@dataclass(frozen=True)
class LLMProvider:
    """
    A single reachable LLM endpoint.

    `name` is a stable internal identifier used in logs and in the
    organization_agent_run.provider_used audit column. It is never derived from
    a URL, so renaming a host doesn't corrupt historical audit rows.
    """

    name: str
    model: str
    base_url: str
    api_key: str

    # Groq supports constrained decoding (`strict: true`), which guarantees
    # schema conformance at the token level. Gemini's OpenAI-compatible
    # endpoint supports json_schema but not Groq's strict mode, so it gets
    # best-effort and leans on post-parse validation instead.
    supports_strict_structured_output: bool = False

    # Rough per-request ceiling. Groq enforces TPM hard; leaving headroom here
    # means we fail over predictably instead of getting cut off mid-generation.
    max_output_tokens: int = 4096

    def build_client(self, timeout: float) -> AsyncOpenAI:
        """
        One AsyncOpenAI per provider, created lazily and reused.

        Reuse matters: a fresh client per call means a fresh connection pool
        per call, which is the difference between one TLS handshake and one per
        agent stage in a multi-stage pipeline. max_retries is deliberately 0 --
        retry policy belongs to the router (so it can fail over between
        providers and honour the circuit breaker), and leaving the SDK's own
        retries on would multiply every attempt behind our back.
        """
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=0,
        )


def _build_chain() -> list:
    """
    Ordered provider chain: primary first, fallbacks after.

    Providers with no configured key are omitted rather than included-and-failed,
    so a single-provider deployment costs nothing at call time. config.py's
    validate_ai_provider_chain() has already guaranteed at least one LLM
    provider exists, so this list is never empty.
    """
    chain = []

    if settings.groq_api_key:
        chain.append(
            LLMProvider(
                name="groq",
                model=settings.groq_organize_model,
                base_url="https://api.groq.com/openai/v1",
                api_key=settings.groq_api_key,
                # gpt-oss-120b is on Groq's strict-mode list. If someone points
                # GROQ_ORGANIZE_MODEL at a model that isn't, the request 400s
                # and the router fails over to Gemini rather than the feature
                # dying — see LLMCallError.failoverable.
                supports_strict_structured_output=True,
                max_output_tokens=8192,
            )
        )

    if settings.gemini_api_key:
        chain.append(
            LLMProvider(
                name="gemini",
                model=settings.gemini_model,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=settings.gemini_api_key,
                supports_strict_structured_output=False,
                max_output_tokens=8192,
            )
        )

    return chain


_provider_chain = _build_chain()


def get_provider_chain() -> list:
    """Read-only view of the configured chain. Order is significant."""
    return list(_provider_chain)


def get_provider(name: str) -> Optional[LLMProvider]:
    for provider in _provider_chain:
        if provider.name == name:
            return provider
    return None