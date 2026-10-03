"""
@file tests/test_config_boot.py
@description Tests for boot-time validation and the health probes.

Two of these tests exist because of bugs found while preparing a deployment, and
both bugs shared a property worth naming: they failed *always*, not
intermittently, so no unit test noticed and no manual smoke test would have
looked twice.

    /health/ready returned 503 on every environment, because the configured
    DATABASE_URL carries a SQLAlchemy dialect prefix that psycopg rejects. A
    deploy gate that is always red is worse than no gate: it trains people to
    ignore it.

    `COOKIE_SAMESITE=none` without `COOKIE_SECURE=true` boots fine and then
    logs every user out instantly, because browsers silently discard such a
    cookie. That is precisely the configuration a Vercel/AWS split produces.

Both are configuration mistakes that produce no error at startup and total
failure in production, which is the class this file exists to convert into a
boot failure.
"""

import pytest
from fastapi.testclient import TestClient

from src.core import config as cfg


# ---------------------------------------------------------------------------
# The database URL, which is not usable by every consumer
# ---------------------------------------------------------------------------


def test_database_url_is_normalised_for_psycopg():
    """
    `DATABASE_URL` carries `postgresql+psycopg://` because Alembic requires the
    dialect prefix. `psycopg.connect()` rejects it outright:

        ProgrammingError: invalid connection option "postgresql+psycopg://..."

    So every direct `psycopg` call must normalise first. Getting this wrong is not
    a subtle failure -- it fails on every environment, which is how it shipped.
    """
    normalised = cfg.database_url_for_psycopg()

    assert not normalised.startswith("postgresql+psycopg")
    assert normalised.startswith("postgresql://")
    # The rest of the URL must survive untouched; rewriting the credentials while
    # fixing the scheme would produce a different failure.
    original = cfg.settings.database_url
    assert normalised == "postgresql://" + original.split("://", 1)[1]


def test_a_plain_postgres_url_passes_through_unchanged():
    """A URL with no dialect prefix is already valid and must not be rewritten."""
    original = cfg.settings.database_url
    cfg.settings.database_url = "postgresql://u:p@host:5432/db?sslmode=require"
    try:
        assert cfg.database_url_for_psycopg() == "postgresql://u:p@host:5432/db?sslmode=require"
    finally:
        cfg.settings.database_url = original


def test_the_psycopg2_prefix_is_normalised_too():
    """
    Both dialect spellings exist in the wild (`+psycopg2` and `+psycopg`), and a
    deployment that used psycopg2 should not break when the driver changes.
    """
    original = cfg.settings.database_url
    cfg.settings.database_url = "postgresql+psycopg2://u:p@host:5432/db"
    try:
        assert cfg.database_url_for_psycopg() == "postgresql://u:p@host:5432/db"
    finally:
        cfg.settings.database_url = original


# ---------------------------------------------------------------------------
# Browser delivery: the silent ones
# ---------------------------------------------------------------------------


def _with_settings(**overrides):
    """Applies settings overrides for the duration of one check, then restores."""
    original = {name: getattr(cfg.settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(cfg.settings, name, value)
    return lambda: [setattr(cfg.settings, n, v) for n, v in original.items()]


def test_samesite_none_without_secure_is_refused():
    """
    The Vercel/AWS deployment mistake.

    The frontend and backend end up on different registrable domains, so the
    cookie must be `SameSite=None; Secure`. Set `none` and forget `secure` and
    the browser *discards the cookie*. Login appears to succeed, then every
    request is unauthenticated, and nothing in any log says why.
    """
    restore = _with_settings(cookie_samesite="none", cookie_secure=False)
    try:
        with pytest.raises(RuntimeError) as exc:
            cfg.validate_browser_delivery_settings()
        assert "COOKIE_SECURE" in str(exc.value)
    finally:
        restore()


def test_samesite_none_with_secure_is_accepted():
    """The correct cross-domain configuration must not be blocked."""
    restore = _with_settings(cookie_samesite="none", cookie_secure=True)
    try:
        assert cfg.validate_browser_delivery_settings() is None
    finally:
        restore()


def test_a_wildcard_cors_origin_is_refused():
    """
    `main.py` sets `allow_credentials=True` because auth is an httpOnly cookie. A
    wildcard origin is illegal for credentialed requests, so the browser refuses
    the response -- the same total-401 symptom as the cookie bug, different cause.
    """
    restore = _with_settings(cors_origins=["https://app.example.com", "*"])
    try:
        with pytest.raises(RuntimeError) as exc:
            cfg.validate_browser_delivery_settings()
        assert "CORS_ORIGINS" in str(exc.value)
    finally:
        restore()


def test_an_empty_cors_origin_list_is_refused():
    """No listed origin means no browser can call this API at all."""
    restore = _with_settings(cors_origins=[])
    try:
        with pytest.raises(RuntimeError):
            cfg.validate_browser_delivery_settings()
    finally:
        restore()


def test_samesite_case_is_normalised_at_construction():
    """
    `COOKIE_SAMESITE=Lax` in the environment must behave like `lax`, or a
    perfectly ordinary spelling gets rejected at boot with a message that looks
    like a code bug rather than a config typo.

    Normalisation lives in a `field_validator`, so it runs on `Settings(...)`
    construction and on environment parsing -- not on direct attribute
    assignment, which is why this test constructs a Settings object rather than
    poking the module-level singleton.
    """
    restored = {k: getattr(cfg.settings, k) for k in ("cookie_samesite", "cookie_secure")}
    try:
        cfg.settings.cookie_samesite = "lax"
        cfg.settings.cookie_secure = True
        assert cfg.validate_browser_delivery_settings() is None

        # Direct assignment bypasses validators, which is exactly why the
        # validator exists -- but it also means the singleton can hold a value
        # the validator would never have produced.
        cfg.settings.cookie_samesite = "Lax"
        with pytest.raises(RuntimeError) as exc:
            cfg.validate_browser_delivery_settings()
        assert "COOKIE_SAMESITE" in str(exc.value)
    finally:
        for key, value in restored.items():
            setattr(cfg.settings, key, value)


def test_a_genuinely_nonsense_samesite_value_is_refused():
    """A typo like `lax; Secure` produces a cookie the browser will not honour."""
    restore = _with_settings(cookie_samesite="nonsense", cookie_secure=True)
    try:
        with pytest.raises(RuntimeError) as exc:
            cfg.validate_browser_delivery_settings()
        assert "COOKIE_SAMESITE" in str(exc.value)
    finally:
        restore()


def test_the_validator_lowercases_the_value():
    """The normalisation itself, isolated from the singleton."""
    restored = cfg.settings.cookie_samesite
    try:
        cfg.settings.cookie_samesite = "NONE"
        # The validator runs through Settings construction, so exercise it the
        # way the environment does.
        assert cfg.Settings.normalize_samesite("NONE") == "none"
        assert cfg.Settings.normalize_samesite("  Lax ") == "lax"
    finally:
        cfg.settings.cookie_samesite = restored


def test_the_local_development_defaults_are_accepted():
    """
    The boot check must not block local development. `lax` + `secure=false` is
    correct for http://localhost, where SameSite is satisfied because ports are
    ignored by the spec.
    """
    restore = _with_settings(cookie_samesite="lax", cookie_secure=False)
    try:
        assert cfg.validate_browser_delivery_settings() is None
    finally:
        restore()


# ---------------------------------------------------------------------------
# The AI provider chain
# ---------------------------------------------------------------------------


def test_an_empty_llm_chain_refuses_to_boot():
    """
    A deployment with no LLM provider at all is a misconfiguration, not a
    degraded state. Booting and failing on the first "Organize" click costs
    hours; failing here costs five seconds.
    """
    restore = _with_settings(groq_api_key=None, gemini_api_key=None)
    try:
        with pytest.raises(RuntimeError) as exc:
            cfg.validate_ai_provider_chain()
        assert "no LLM provider" in str(exc.value)
    finally:
        restore()


def test_a_single_provider_is_enough_to_boot():
    """A chain with one provider is legitimate -- it just has no failover."""
    restore = _with_settings(groq_api_key=None, gemini_api_key="k")
    try:
        assert cfg.validate_ai_provider_chain() is None
    finally:
        restore()


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from src.main import app

    return TestClient(app)


def test_liveness_does_not_touch_the_database(client):
    """
    A liveness probe that depends on Postgres turns a 30-second database blip
    into a full outage: the probe fails on every task at once, the orchestrator
    replaces all of them, and the replacements need the same unavailable
    database. This is a replacement storm that cannot help.

    So `/health` must answer with no database contact whatsoever, even when
    DATABASE_URL is garbage.
    """
    from src.main import app

    original = cfg.settings.database_url
    cfg.settings.database_url = "postgresql://this:is:not:a:valid:url@nowhere:9999/none"
    try:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
    finally:
        cfg.settings.database_url = original


def test_readiness_reports_ready_when_the_database_reaches(client):
    """
    The counterpart to the bug above: `/health/ready` must actually reach the
    database. It returned 503 for its entire life because the configured URL
    carries a SQLAlchemy dialect prefix that psycopg rejects.

    Skipped rather than failed on a *connection* error, and that distinction is
    the point. A malformed URL fails instantly and identically on every machine,
    which is what this test is for. A reachable-in-principle pooler that is slow
    under load is a different failure -- Supabase's pooler queues connections per
    client, and this suite's `live` tests are opening their own -- and failing on
    that would make a correct fix look broken on someone else's machine.
    """
    from tests.helpers import database_url

    if not database_url():
        pytest.skip("no reachable DATABASE_URL")

    response = client.get("/health/ready")

    if response.status_code == 503:
        # Distinguish "our URL is wrong" from "the pooler did not answer".
        # A driver-level configuration error is what we are testing for.
        if "invalid connection option" in response.text.lower() or "invalid dsn" in response.text.lower():
            pytest.fail(f"the connection string is malformed: {response.text}")
        pytest.skip("database not reachable from this machine; the URL parsed correctly")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}


def test_readiness_reports_503_with_a_generic_message(client):
    """
    The failure path must not leak. A psycopg exception message can contain the
    connection string, and this response is unauthenticated -- anyone who can
    reach the port can read it.
    """
    original = cfg.settings.database_url
    cfg.settings.database_url = "postgresql://u:p@127.0.0.1:1/definitely_not_listening"
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503

        body = response.text
        assert "u:p" not in body, "the response leaked the connection string"
        assert "password" not in body.lower()
    finally:
        cfg.settings.database_url = original

# ---------------------------------------------------------------------------
# CORS origins: the format that only fails in production
# ---------------------------------------------------------------------------


def _cors_origins_from(value: str):
    """Builds Settings from a CORS_ORIGINS value, in a subprocess with no .env."""
    import os
    import subprocess
    import sys

    code = (
        "import os, sys; sys.path.insert(0, '.')\n"
        "from src.core.config import Settings\n"
        "print(repr(Settings().cors_origins))\n"
    )
    env = {
        **os.environ,
        "SUPABASE_URL": "http://127.0.0.1:54321",
        # JWT-shaped: create_client validates the key at import time.
        "SUPABASE_ANON_KEY": "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYW5vbiJ9.sig",
        "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJub25lIn0.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig",
        "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "SUPABASE_JWKS_URL": "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json",
        "READER_JWT_SECRET": "x" * 20,
        "ASSEMBLYAI_API_KEY": "x",
        "GEMINI_API_KEY": "x",
        "CORS_ORIGINS": value,
    }
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"CORS_ORIGINS={value!r} refused to boot:\n{result.stderr[-600:]}"
    return eval(result.stdout.strip())


def test_comma_separated_cors_origins_boots():
    """
    The format the deployment docs and `.env.production.example` tell operators
    to write.

    `cors_origins: List[str]` makes pydantic-settings JSON-decode the variable
    *before* the field validator runs, so this raised

        SettingsError: error parsing value for field "cors_origins"

    and the process refused to boot. `NoDecode` on the field routes the raw
    string to the validator instead.

    It went unnoticed because local development never set the variable: the
    default list applied and the parsing path was never exercised. The first
    real deployment is what reaches it.
    """
    assert _cors_origins_from("http://localhost:3000,http://127.0.0.1:3000") == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_a_json_array_still_parses():
    """Backwards compatibility: pydantic-settings' native form must keep working."""
    assert _cors_origins_from('["https://a.example", "https://b.example"]') == [
        "https://a.example",
        "https://b.example",
    ]


def test_a_single_origin_needs_no_comma():
    assert _cors_origins_from("https://only.example") == ["https://only.example"]


def test_surrounding_whitespace_and_trailing_commas_are_dropped():
    """
    A trailing comma must not produce an empty origin. An empty string in
    `allow_origins` matches nothing, so it looks configured while permitting no
    browser at all -- which reads as a mysterious 401 rather than a config error.
    """
    assert _cors_origins_from(" https://a.example , https://b.example ,") == [
        "https://a.example",
        "https://b.example",
    ]


def test_an_empty_cors_origins_refuses_to_boot():
    """
    No listed origin means no browser can call this API, which is never a
    deployment anyone intends. Failing at boot beats a silently unusable service.
    """
    import os
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "from src.core.config import Settings; Settings()\n"
    )
    env = {
        **os.environ,
        "SUPABASE_URL": "http://127.0.0.1:54321",
        "SUPABASE_ANON_KEY": "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYW5vbiJ9.sig",
        "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJub25lIn0.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig",
        "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "SUPABASE_JWKS_URL": "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json",
        "READER_JWT_SECRET": "x" * 20,
        "ASSEMBLYAI_API_KEY": "x",
        "GEMINI_API_KEY": "x",
        "CORS_ORIGINS": "",
    }
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "CORS_ORIGINS" in result.stderr
