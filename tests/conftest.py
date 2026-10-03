"""
@file tests/conftest.py
@description Test configuration. Establishes environment BEFORE src is imported.

WHY THE ENV IS SET HERE AND NOT IN A FIXTURE

`src/core/config.py` builds `settings = Settings()` at import time and then calls
`validate_ai_provider_chain()`, which raises when no LLM provider key is
present. That is the correct production behaviour — a misconfigured deployment
must refuse to boot — and it means the environment has to be in place before
`conftest` finishes importing, not before the first test runs.

The values below are dummies. `SUPABASE_URL` is syntactically a URL because
`supabase_client.py` constructs a client at import time and the client validates
its own URL; nothing connects during a test.

`GROQ_API_KEY` is set so the chain validates as a two-provider chain. Tests that
care about which providers are configured override it explicitly rather than
relying on this default.

Nothing here mocks a network call. There is no HTTP interception anywhere in
this suite: every provider and repository test replaces the function under test
with a local fake, so a test cannot pass because a mock was wired up too
leniently to catch it.
"""

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _dummy_jwt(role: str) -> str:
    """
    Builds a structurally valid, unsigned JWT.

    Not decoration. `supabase.create_client` validates the key with a regex at
    import time and raises `SupabaseException("Invalid API key")` before any
    request is made, so a placeholder like "test-key" makes every module that
    imports `supabase_admin` unimportable -- which is most of the suite.

    The signature segment is not a real signature and nothing verifies it: no
    test authenticates against Supabase. It just has to survive the regex, and
    a real base64url payload means a future version of the client that decodes
    the claims does not break collection either.
    """
    import base64
    import json

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        [
            seg({"alg": "none", "typ": "JWT"}),
            seg({"role": role, "iss": "test", "aud": "test"}),
            "not-a-real-signature",
        ]
    )


_TEST_ENV = {
    "SUPABASE_URL": "http://127.0.0.1:54321",
    "SUPABASE_ANON_KEY": _dummy_jwt("anon"),
    "SUPABASE_SERVICE_ROLE_KEY": _dummy_jwt("service_role"),
    "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
    "SUPABASE_JWKS_URL": "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json",
    "READER_JWT_SECRET": "test-reader-jwt-secret-not-used-in-tests",
    "ASSEMBLYAI_API_KEY": "test-assemblyai-key",
    "GEMINI_API_KEY": "test-gemini-key",
    "GROQ_API_KEY": "test-groq-key",
    "DEEPGRAM_API_KEY": "test-deepgram-key",
}

# `setdefault`, not assignment: a developer with real credentials in their shell
# who runs `pytest` should exercise their own chain, not be silently overridden.
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)


import pytest  # noqa: E402  -- must follow the env setup above


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """
    Replaces time.sleep in the modules under test with a no-op.

    Only applied to modules that are actually imported here. This is not about
    speed for its own sake: a retry-backoff test that really sleeps is a test
    that takes 30 seconds and gets deleted by the next person under time
    pressure, and the coverage goes with it.
    """
    import asyncio
    import time

    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    async def _instant_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# Supabase fake
#
# Repository code is written against the PostgREST query builder, which is a
# chain of terminal and non-terminal calls ending in `.execute()`. Monkeypatching
# each method individually is brittle: a new `.order()` in the code under test
# would AttributeError on a hand-rolled stub rather than failing an assertion.
#
# So this records the whole chain. A test asserts on `fake.calls`, which reads as
# the actual query that would have been sent, and an unrecognised builder method
# is a loud failure rather than a silent no-op.
#
# Deliberately not `unittest.mock`: there is no HTTP interception anywhere in
# this suite, so a test cannot pass because a mock was wired up too leniently to
# catch the thing it was written to catch.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Terminal result of a fake query."""

    def __init__(self, data):
        self.data = data


class _FakeQueryBuilder:
    """Records every builder call; returns itself until `.execute()`."""

    #: Read builder methods. Chainable, return self, record the call.
    READ_METHODS = {
        "select", "eq", "neq", "gt", "gte", "lt", "lte", "in_", "is_", "like",
        "order", "limit", "range", "single", "maybe_single", "count",
    }
    #: Write builder methods. Also set the verb on the query, because the fake
    #: has to report what would actually have been sent.
    WRITE_METHODS = {"insert", "update", "upsert", "delete"}

    def __init__(self, fake, table):
        self._fake = fake
        self._table = table
        self._verb = "select"
        self._filters = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        if name in self.READ_METHODS or name in self.WRITE_METHODS:
            return self._bind(name)
        raise AttributeError(
            f"FakeQueryBuilder does not model {name!r}. If production code now "
            f"uses a builder method this fake does not know, add it explicitly "
            f"rather than letting the test pass on a silent no-op."
        )

    def _bind(self, name):
        def _record(*args, **kwargs):
            if name in self.WRITE_METHODS:
                self._verb = name
                # The rows being written. Asserting on this is how a test proves
                # `memory.body_text` was never in an update payload.
                self._fake.writes.append({"table": self._table, "verb": name, "rows": args[0] if args else None})
            else:
                # Arguments rendered so a test can assert the *values* were
                # scoped, not merely that a filter existed. An unscoped write is
                # the cross-tenant bug class this suite exists to catch, and
                # "eq was called" is not enough to prove it wasn't `.eq("id", ..)`
                # alone.
                rendered = ", ".join(repr(a) for a in args)
                self._filters.append(f"{name}({rendered})")
            return self

        return _record

    def execute(self):
        self._fake.calls.append(
            {
                "table": self._table,
                "verb": self._verb,
                "filters": list(self._filters),
            }
        )
        return _FakeResponse(self._fake.results.get(self._table, []))


class FakeSupabase:
    """
    Stand-in for the module-level `supabase_admin` client.

    `table(name)` returns a fresh builder per call, matching the real client and
    keeping one test's filters from leaking into the next.

    `results` maps a table name to the rows `.execute()` should return, so a test
    can drive the repository's branches by what the database would say.

    `calls` records reads (with their filters). `writes` records writes (with
    their rows). Kept separate because the questions are different: "was this
    read scoped?" and "what exactly did we write?" — an assertion that scans one
    list for both will miss things.
    """

    def __init__(self, results=None):
        self.calls = []
        self.writes = []
        self.results = results or {}

    def table(self, name):
        return _FakeQueryBuilder(self, name)


@pytest.fixture
def fake_supabase():
    """A fresh recording fake. Shape results with `FakeSupabase(results={...})`."""
    return FakeSupabase()
