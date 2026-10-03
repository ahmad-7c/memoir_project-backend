# syntax=docker/dockerfile:1
#
# Memoir backend image.
#
# Two-stage build: dependencies are compiled and installed into a self-contained
# virtualenv in the builder, then only that virtualenv is copied into the
# runtime stage. The runtime image carries no compiler, no pip cache, no build
# toolchain and no source history -- it has the interpreter, the wheels, and the
# application.
#
# ---------------------------------------------------------------------------
# THIS IMAGE DOES NOT RUN MIGRATIONS AT STARTUP. DELIBERATELY.
#
# Three revisions in the chain ahead of the current head are not idempotent:
#
#     879e2c8d1b8c  ADD CONSTRAINT
#     080151e0f1e8  DROP CONSTRAINT
#     cd3760273110  DROP COLUMN + ADD COLUMN   <-- erases organize history
#
# `alembic upgrade head` in an entrypoint turns "the database is at an unexpected
# revision" into data loss with no rollback, triggered by whichever deploy was
# meant to fix something else. Migrate as an explicit, separate step:
#
#     docker run --rm --env-file .env.production <image> python scripts/migrate.py
#
# which reads alembic_version first and refuses from any revision it cannot
# prove is safe to advance. See deploy/README.md.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# `uvicorn[standard]` pulls uvloop/httptools/watchfiles, which need a toolchain
# to build from source when no matching wheel exists for the interpreter. They
# normally do ship wheels; the build-essential layer is insurance for a
# platform without one, and it never reaches the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copied on its own so this layer is cached until a dependency actually changes.
# Editing application code does not re-resolve the dependency tree.
COPY requirements.txt ./
RUN pip install --requirement requirements.txt

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# libpq is the only shared library the installed wheels need at runtime.
# curl/wget are deliberately absent -- the healthcheck uses the interpreter, so
# there is no reason to ship an HTTP client to a network-facing container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 memoir \
    && useradd --system --uid 1001 --gid memoir --no-create-home --shell /usr/sbin/nologin memoir

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Application code and the Alembic tree. `.env` is excluded by .dockerignore --
# secrets belong in the orchestrator's environment, never in an image layer,
# where they survive every subsequent push to a registry.
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY src ./src

# Owned by root and not writable by the service account: a compromised request
# cannot overwrite application code and have it persist into the next task.
RUN chown -R root:root /app \
    && chmod -R go-w /app

USER 1001:1001

EXPOSE 8000

# Liveness only -- deliberately does not touch Postgres. See /health in main.py
# for why a database-dependent probe turns a database blip into a task-replacement
# storm. Wire /health/ready into the deploy gate or an alarm instead.
#
# ECS uses this when the task definition defines no healthCheck override; an ALB
# target group needs its own path configured separately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# --proxy-headers / --forwarded-allow-ips: behind an ALB or CloudFront the socket
#   peer is the load balancer, so without this every request appears to originate
#   from one IP and IP-keyed rate limiting stops meaning anything.
# --workers: the organize pipeline is a BackgroundTask that occupies a threadpool
#   slot for up to ORGANIZE_PIPELINE_DEADLINE_SECONDS. Each worker has its own
#   event loop and its own threadpool, so workers scale that capacity. Note the
#   in-process chat rate limiter is per-worker (known gap) -- do not raise the
#   worker count expecting the limit to scale with it.
# --timeout-keep-alive: must exceed the ALB idle timeout, or the proxy drops
#   connections the server still considers live.
CMD ["sh", "-c", "exec uvicorn src.main:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --workers ${WEB_CONCURRENCY:-2} \
    --proxy-headers \
    --forwarded-allow-ips '*' \
    --timeout-keep-alive 65 \
    --log-level info"]