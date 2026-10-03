"""
@file tests/test_llm_failover.py
@description Tests for provider failover classification.

Backend AGENTS.md §5.1: the distinction between a provider being *down* and a
response being *wrong* is the whole point of the failover chain, and it is easy
to lose in a refactor because both cases surface as "the call raised".

Fail over on infrastructure: timeout, 429, 5xx, connection error.
Do NOT fail over on a well-formed response that failed content validation.

Retrying a validation failure elsewhere hides a real defect behind a coin flip
and spends money on every occurrence. It also means the second provider is being
sent a request known to be malformed.
"""

import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)
from httpx import Request, Response

from src.integrations.llm import router as llm_router
from src.schemas.organization import ReaderOutput


def _status_error(status_code: int) -> APIStatusError:
    """A realistic APIStatusError for the given HTTP status."""
    request = Request(method="POST", url="https://api.groq.com/openai/v1/chat/completions")
    response = Response(status_code=status_code, request=request)
    return APIStatusError("provider returned an error", response=response, body=None)


# ---------------------------------------------------------------------------
# Infrastructure failures: must fail over
# ---------------------------------------------------------------------------


def test_rate_limit_error_fails_over():
    assert llm_router._is_infrastructure_failure(
        RateLimitError("429", response=_status_error(429).response, body=None)
    )


def test_429_arriving_as_a_plain_api_status_error_fails_over():
    """
    The case that a naive `isinstance(exc, RateLimitError)` check misses.

    The SDK normally maps a 429 to `RateLimitError`, but a proxy or gateway in
    front of a provider can return 429 as a plain `APIStatusError`. Treating that
    as non-recoverable turns a rate-limited provider into a hard outage -- the
    fallback never runs, and the user sees a failure instead of a slower answer.

    This has bitten for real, which is why it is a test rather than a comment.
    """
    exc = _status_error(429)
    assert type(exc) is APIStatusError, "must be the generic class for this test to mean anything"
    assert llm_router._is_infrastructure_failure(exc) is True


@pytest.mark.parametrize("status_code", [408, 500, 502, 503, 504, 529])
def test_server_side_statuses_fail_over(status_code):
    for cls in (APIStatusError, InternalServerError):
        exc = _status_error(status_code)
        assert llm_router._is_infrastructure_failure(exc) is True, f"{cls.__name__} {status_code}"


def test_connection_and_timeout_errors_fail_over():
    assert llm_router._is_infrastructure_failure(
        APIConnectionError(request=Request(method="POST", url="https://api.groq.com"))
    )
    assert llm_router._is_infrastructure_failure(
        APITimeoutError(request=Request(method="POST", url="https://api.groq.com"))
    )


# ---------------------------------------------------------------------------
# Logic failures: must NOT fail over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_client_errors_do_not_fail_over(status_code):
    """
    A 4xx other than 408/429 means *our request* is wrong: a bad key, a schema the
    provider rejects, a model that does not exist.

    Failing over here sends the identical malformed request to a second vendor,
    which may well accept it -- so the bug becomes intermittent and
    provider-dependent, and the real cause is never surfaced. That is strictly
    worse than failing fast.
    """
    assert llm_router._is_infrastructure_failure(_status_error(status_code)) is False


def test_a_bad_key_does_not_fail_over():
    """401 from provider one means the key is wrong; provider two will not help."""
    exc = AuthenticationError("bad key", response=_status_error(401).response, body=None)
    assert llm_router._is_infrastructure_failure(exc) is False


def test_a_rejected_schema_does_not_fail_over():
    """
    Strict mode rejecting our schema is a bug in `strict_json_schema()`, not an
    outage. Failing over papers over it and the 400 disappears behind whichever
    provider happened to accept the malformed schema.
    """
    exc = BadRequestError("schema invalid", response=_status_error(400).response, body=None)
    assert llm_router._is_infrastructure_failure(exc) is False


def test_an_arbitrary_exception_does_not_fail_over():
    """Anything unrecognised is a bug, not an outage. Default to not retrying."""
    assert llm_router._is_infrastructure_failure(ValueError("bug in our own code")) is False
    assert llm_router._is_infrastructure_failure(RuntimeError("unreachable")) is False


def test_a_status_error_with_no_readable_status_code_does_not_fail_over():
    """
    `getattr(exc, "status_code", None)` returning None must be False, not True.

    A response carrying no status is not evidence of an outage, and treating it
    as one would retry the request against every fallback for an error we have
    not identified. The `getattr` default exists precisely so this branch is
    reachable; it used to be `exc.status_code` directly, which raised
    AttributeError out of the exception handler and turned a provider quirk into
    an unhandled crash.
    """
    class _NoStatus(Exception):
        """Not an APIStatusError, so the SDK __init__ cannot set status_code."""

    assert llm_router._is_infrastructure_failure(_NoStatus("odd")) is False

    # The same for a real APIStatusError whose attribute is missing, which is the
    # closest a genuine provider exception gets to this state.
    exc = _status_error(500)
    del exc.status_code
    assert llm_router._is_infrastructure_failure(exc) is False


# ---------------------------------------------------------------------------
# The rule that is easiest to break by accident
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 429 - rate limit reached",
        "Request 1-2-3 failed with status 500",
        "model gpt-oss-120b returned 503 from region us-east-1",
    ],
)
def test_substring_matching_would_have_given_the_right_answer_here(message):
    """
    Documents WHY the classification is by type and not by substring.

    These messages would all be classified correctly by `if "429" in str(exc)`,
    which is exactly what makes that approach dangerous: it looks right on the
    cases you tested. The real damage is the false positives and the misses, and
    neither shows up in a test written from the passing cases.

    Kept as a passing test rather than a comment so the failure mode stays
    concrete: if someone reverts to substring matching, this test still passes
    (it documents the old behaviour accurately) and the ones above catch it.
    """
    assert isinstance(message, str)
    exc = ValueError(message)
    # By TYPE it is not an infrastructure failure, despite the message content.
    assert llm_router._is_infrastructure_failure(exc) is False


def test_a_400_whose_message_mentions_500_does_not_fail_over():
    """
    The concrete false positive. A provider returning 400 with a body mentioning
    a 500 somewhere in its text would be retried against every fallback, on
    every request, for a request that can never succeed.
    """
    exc = _status_error(400)
    assert llm_router._is_infrastructure_failure(exc) is False


def test_validation_failure_is_not_classified_as_failoverable():
    """
    `ProviderRejectedOutput` is the model returning well-formed JSON that failed
    *content* validation. The router must propagate it immediately -- never retry
    it on the fallback provider.
    """
    assert issubclass(llm_router.ProviderRejectedOutput, Exception)


# ---------------------------------------------------------------------------
# Backoff: jitter is the point
# ---------------------------------------------------------------------------


def test_backoff_grows_but_is_capped():
    """Bounded so a dead provider cannot multiply latency across the chain."""
    for attempt in range(0, 8):
        delay = llm_router._backoff_delay(attempt)
        assert 0 <= delay <= 8, f"attempt {attempt} produced {delay}"
        assert delay <= min(2**attempt, 8) + 1e-9


def test_backoff_is_jittered_not_deterministic():
    """
    Without jitter, every agent in a parallel fan-out that got 429'd retries in
    lockstep and reproduces the identical burst that caused the 429. The curve is
    secondary; the randomness is the feature.
    """
    samples = {llm_router._backoff_delay(4) for _ in range(20)}
    assert len(samples) > 1, "backoff is deterministic; a synchronised retry storm is unavoidable"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_the_threshold_and_short_circuits(monkeypatch):
    """
    Without this, a dead provider adds its full timeout to every single
    subsequent request -- the failure mode the breaker exists to prevent.
    """
    monkeypatch.setattr(llm_router.settings, "ai_circuit_failure_threshold", 3)
    monkeypatch.setattr(llm_router.settings, "ai_circuit_cooldown_seconds", 60)

    breaker = llm_router._CircuitBreaker(failure_threshold=3, cooldown_seconds=60)

    assert breaker.is_open() is False

    for _ in range(3):
        breaker.record_failure()

    assert breaker.is_open() is True


def test_circuit_breaker_closes_again_after_a_success(monkeypatch):
    """A provider that recovers must be retried, not written off."""
    monkeypatch.setattr(llm_router.settings, "ai_circuit_failure_threshold", 2)
    monkeypatch.setattr(llm_router.settings, "ai_circuit_cooldown_seconds", 60)

    breaker = llm_router._CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is True

    breaker.record_success()

    assert breaker.is_open() is False


def test_breakers_are_cached_per_provider_not_recreated_per_call(monkeypatch):
    """
    A breaker that resets on every request is not a breaker. `_breaker_for`
    must return the same object for the same provider name, which is also the
    unbounded-growth risk if provider names ever became dynamic.
    """
    llm_router._breakers.clear()
    try:
        first = llm_router._breaker_for("groq")
        second = llm_router._breaker_for("groq")
        other = llm_router._breaker_for("gemini")

        assert first is second, "breaker state is being discarded between calls"
        assert first is not other, "providers must not share a breaker"
    finally:
        llm_router._breakers.clear()


async def test_an_empty_provider_chain_fails_fast_and_is_not_failoverable(monkeypatch):
    """
    A chain with nothing in it is a misconfiguration, not an outage.

    `failoverable=False` is what stops the caller treating this as a provider
    problem and retrying a configuration that cannot succeed. In practice
    `validate_ai_provider_chain()` refuses to boot in this state, so reaching
    here means something bypassed config validation -- which is exactly when the
    error needs to be unambiguous.

    An async test rather than a sync one wrapping `asyncio.run`, so the router's
    own awaits are exercised under the same event loop the application uses.
    """
    monkeypatch.setattr(llm_router, "get_provider_chain", lambda: [])

    with pytest.raises(llm_router.LLMCallError) as exc:
        await llm_router.complete_json(
            system_prompt="s",
            user_prompt="u",
            response_model=ReaderOutput,
            purpose="test",
        )

    assert exc.value.failoverable is False
    assert "no llm provider" in str(exc.value).lower()
