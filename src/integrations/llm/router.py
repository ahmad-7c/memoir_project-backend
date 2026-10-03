"""
@file integrations/llm/router.py
@description The single entry point every LLM call in this codebase goes
through. Owns failover between providers, retry/backoff, and a per-provider
circuit breaker.

The failover rule that matters most:

    A provider is failed over on INFRASTRUCTURE failure (timeout, 429, 5xx,
    connection error, empty/None response). It is NOT failed over because the
    model returned a well-formed response that failed content validation.

That distinction is deliberate. "The model returned a memory_id we never sent"
is a bug or a confused model — not an outage. Retrying it on another provider
hides the defect behind a coin flip and spends money on every occurrence. The
reference implementation made exactly this mistake: its fallback loop retried
seven models in sequence on *any* exception, so a genuine logic bug looked
identical to a provider outage and burned seven calls before giving up.
"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional, Type

from src.core.config import settings
from src.integrations.llm.provider import LLMProvider, get_provider_chain

logger = logging.getLogger(__name__)


class LLMCallError(Exception):
    """
    Raised when the whole chain failed. `provider` names the last provider
    tried, for logging — never surfaced to the caller.
    """

    def __init__(self, message: str, provider: Optional[str] = None, failoverable: bool = True):
        super().__init__(message)
        self.provider = provider
        self.failoverable = failoverable


class ProviderRejectedOutput(Exception):
    """
    Raised when a provider returned a *usable* response whose content failed
    our validation.

    Deliberately distinct from LLMCallError: this never triggers failover. The
    caller decides what to do, because only the caller knows whether the
    response is discardable (an organizer proposal is) or load-bearing.
    """


class _CircuitBreaker:
    """
    Per-provider failure counter with a cooldown.

    Without this, a dead primary means every single request pays the primary's
    full timeout before failing over. On a 30s timeout that is a 30-second
    penalty on every organize request, forever, for a provider that is not
    coming back. The breaker converts that into one timeout per cooldown window.
    """

    def __init__(self, failure_threshold: int, cooldown_seconds: int):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: Optional[float] = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            # Cooldown elapsed: allow one probe through. Reset on that probe's
            # outcome rather than optimistically closing now, so a provider
            # that is still down doesn't get a free pass every cooldown.
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning(
                "LLM circuit opened for provider after %d consecutive failures "
                "(cooldown %ds).",
                self._failures,
                self.cooldown_seconds,
            )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None


_breakers: Dict[str, _CircuitBreaker] = {}


def _breaker_for(provider_name: str) -> _CircuitBreaker:
    breaker = _breakers.get(provider_name)
    if breaker is None:
        breaker = _CircuitBreaker(
            failure_threshold=settings.ai_circuit_failure_threshold,
            cooldown_seconds=settings.ai_circuit_cooldown_seconds,
        )
        _breakers[provider_name] = breaker
    return breaker


def _is_infrastructure_failure(exc: Exception) -> bool:
    """
    Classifies by exception TYPE, never by substring-matching the message.

    The previous implementation did `if "429" in str(exc)`, which both misses
    real 429s (SDK messages vary by version) and produces false positives (any
    error whose text happens to contain those digits, including an echoed id).
    """
    try:
        from openai import APITimeoutError, APIConnectionError, RateLimitError
        from openai import APIStatusError
    except ImportError:  # pragma: no cover - openai is a hard dependency
        return False

    if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        # 429 is checked by STATUS, not by exception class. The SDK normally
        # maps a 429 to RateLimitError, but a proxy or gateway in front of a
        # provider can return 429 as a plain APIStatusError — and treating that
        # as non-recoverable means a rate-limited provider becomes a hard
        # outage instead of a failover.
        #
        # Everything else that is 4xx means our request is wrong (bad key, bad
        # schema, unsupported model). Retrying those just sends the same broken
        # request to a second vendor and hides the real cause.
        status = getattr(exc, "status_code", None)
        if status is None:
            return False
        return status == 429 or status == 408 or status >= 500
    return False


_client_cache: Dict[str, Any] = {}


def _client_for(provider: LLMProvider):
    client = _client_cache.get(provider.name)
    if client is None:
        client = provider.build_client(timeout=settings.ai_request_timeout_seconds)
        _client_cache[provider.name] = client
    return client


def _backoff_delay(attempt: int) -> float:
    """
    Exponential backoff with full jitter.

    Jitter matters more than the curve: without it, every agent in a parallel
    fan-out that gets 429'd retries in lockstep and reproduces the same burst.
    """
    ceiling = min(2 ** attempt, 8)
    return random.uniform(0, ceiling)


async def complete_json(
    *,
    system_prompt: str,
    user_prompt: str,
    response_model: Type,
    purpose: str,
) -> Any:
    """
    Calls the chain in order and returns a validated `response_model` instance.

    Returns the parsed Pydantic object, never a raw dict — callers cannot
    accidentally skip validation, and there is no path where a partially-trusted
    shape escapes into the domain layer.

    `purpose` is a short label used only for log correlation ("organizer",
    "reader", "chat"). It must never contain user content.
    """
    chain = get_provider_chain()
    if not chain:
        raise LLMCallError("No LLM providers configured.", failoverable=False)

    last_error: Optional[Exception] = None
    attempted: List[str] = []

    for provider in chain:
        breaker = _breaker_for(provider.name)

        if breaker.is_open():
            logger.info(
                "Skipping LLM provider '%s' for %s: circuit open.", provider.name, purpose
            )
            continue

        attempted.append(provider.name)

        for attempt in range(1, settings.ai_max_attempts_per_provider + 1):
            try:
                result = await _attempt(
                    provider=provider,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_model=response_model,
                )
                breaker.record_success()
                if len(attempted) > 1 or attempt > 1:
                    logger.info(
                        "LLM %s succeeded on '%s' (attempt %d).", purpose, provider.name, attempt
                    )
                return result

            except ProviderRejectedOutput:
                # A usable response with unusable content. Failover is the wrong
                # answer — see the module docstring. Propagate immediately.
                raise

            except Exception as exc:  # noqa: BLE001 - classified immediately below
                if not _is_infrastructure_failure(exc):
                    # Not an outage: a malformed request, a bad schema, an auth
                    # failure. Retrying on the next provider sends the same
                    # broken request to another vendor and hides the cause.
                    breaker.record_failure()
                    raise LLMCallError(
                        f"LLM call for {purpose} failed non-recoverably on "
                        f"'{provider.name}': {type(exc).__name__}",
                        provider=provider.name,
                        failoverable=False,
                    ) from exc

                last_error = exc
                logger.warning(
                    "LLM %s attempt %d/%d failed on '%s' (%s).",
                    purpose,
                    attempt,
                    settings.ai_max_attempts_per_provider,
                    provider.name,
                    type(exc).__name__,
                )
                if attempt < settings.ai_max_attempts_per_provider:
                    await asyncio.sleep(_backoff_delay(attempt))

        breaker.record_failure()

    detail = ", ".join(attempted) if attempted else "none (all circuits open)"
    raise LLMCallError(
        f"LLM chain exhausted for {purpose}. Providers tried: {detail}.",
        provider=attempted[-1] if attempted else None,
    ) from last_error


async def _attempt(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    response_model: Type,
) -> Any:
    """One provider, one call, one parse. All error policy lives in the caller."""
    client = _client_for(provider)

    request: Dict[str, Any] = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": _response_format_for(provider, response_model),
        "max_tokens": provider.max_output_tokens,
        # Low temperature: this is a classification/extraction task, not a
        # creative one. The reference implementation used 0.6, which makes
        # grouping decisions less reproducible run to run.
        "temperature": 0.2,
    }

    response = await client.chat.completions.create(**request)

    # Defensive: an empty choices list is a real provider behaviour (content
    # filtering), not a theoretical one. Indexing [0] blindly raises
    # IndexError, which the caller would misread as a bug rather than a
    # filter event.
    if not response.choices:
        raise LLMCallError(
            f"Provider '{provider.name}' returned no choices (likely content filtering).",
            provider=provider.name,
        )

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise LLMCallError(
            f"Provider '{provider.name}' returned empty content.",
            provider=provider.name,
        )

    # Markdown-fence stripping is retained ONLY for non-strict providers.
    # Under Groq strict mode the model is constrained at the token level and
    # cannot emit fences, so this is dead weight there — but best-effort
    # providers still occasionally wrap JSON, and stripping is strictly better
    # than a parse failure that discards a good response.
    if not provider.supports_strict_structured_output:
        content = _strip_code_fence(content)

    try:
        return response_model.model_validate_json(content)
    except Exception as parse_err:
        raise ProviderRejectedOutput(
            f"{provider.name} response failed schema validation: {parse_err}"
        ) from parse_err


def _strip_code_fence(content: str) -> str:
    """Removes ```json ... ``` wrapping if present. Never raises."""
    text = content.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _response_format_for(provider: LLMProvider, response_model: Type) -> Dict[str, Any]:
    """
    Builds the response_format for this provider's capabilities.

    Strict mode has hard schema requirements (every field required,
    additionalProperties false). Our contracts use Optional fields with
    defaults for good reasons — an omitted inferred_date is meaningful — so
    schemas carry a `strict_json_schema()` companion built in
    schemas/organization.py. That companion is used here; the Pydantic model
    itself stays ergonomic for Python-side construction and validation.
    """
    if provider.supports_strict_structured_output:
        strict_schema = getattr(response_model, "strict_json_schema", None)
        if strict_schema is not None:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": strict_schema(),
                    "strict": True,
                },
            }

    # Best-effort mode: JSON mode only guarantees valid JSON, not valid schema.
    return {"type": "json_object"}


async def complete_text(
    *,
    system_prompt: str,
    messages: List[Dict[str, str]],
    purpose: str,
) -> str:
    """
    Plain-text completion, used by the conversational archive chat.

    Separate from complete_json because chat returns prose and has no schema —
    but it shares the same failover and breaker policy, because "Groq is down"
    is a transport fact that doesn't care what we're asking for.
    """
    chain = get_provider_chain()
    if not chain:
        raise LLMCallError("No LLM providers configured.", failoverable=False)

    last_error: Optional[Exception] = None
    attempted: List[str] = []

    for provider in chain:
        breaker = _breaker_for(provider.name)
        if breaker.is_open():
            continue

        attempted.append(provider.name)
        for attempt in range(1, settings.ai_max_attempts_per_provider + 1):
            try:
                client = _client_for(provider)
                response = await client.chat.completions.create(
                    model=provider.model,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    max_tokens=provider.max_output_tokens,
                    temperature=0.7,
                )
                if not response.choices:
                    raise LLMCallError(
                        f"Provider '{provider.name}' returned no choices.",
                        provider=provider.name,
                    )
                content = response.choices[0].message.content
                if not content or not content.strip():
                    raise LLMCallError(
                        f"Provider '{provider.name}' returned empty content.",
                        provider=provider.name,
                    )
                breaker.record_success()
                return content.strip()

            except Exception as exc:  # noqa: BLE001 - classified immediately below
                if not _is_infrastructure_failure(exc):
                    breaker.record_failure()
                    raise LLMCallError(
                        f"LLM chat failed non-recoverably on '{provider.name}': "
                        f"{type(exc).__name__}",
                        provider=provider.name,
                        failoverable=False,
                    ) from exc

                last_error = exc
                logger.warning(
                    "LLM chat attempt %d/%d failed on '%s' (%s).",
                    attempt,
                    settings.ai_max_attempts_per_provider,
                    provider.name,
                    type(exc).__name__,
                )
                if attempt < settings.ai_max_attempts_per_provider:
                    await asyncio.sleep(_backoff_delay(attempt))

        breaker.record_failure()

    raise LLMCallError(
        f"LLM chain exhausted for {purpose}. Providers tried: "
        f"{', '.join(attempted) if attempted else 'none (all circuits open)'}.",
        provider=attempted[-1] if attempted else None,
    ) from last_error


def reset_breakers() -> None:
    """
    Test/ops affordance: closes all circuits and clears per-provider clients.

    Not called in production paths — a breaker that resets itself defeats its
    own purpose.
    """
    _breakers.clear()
    _client_cache.clear()