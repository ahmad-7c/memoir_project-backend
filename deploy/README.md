# Deploying the Memoir backend to AWS

Primary target: **ECS Fargate behind an Application Load Balancer**, with Supabase Postgres as
the database. The same image runs unchanged on App Runner or EC2 — the container contract is the
interface, and `docker-compose.yml` is here so you can prove that before touching AWS.

The frontend goes to Vercel separately. Nothing in this document deploys it; §7 lists the env
vars it needs.

---

## 0. What is and is not in this image

| | |
|---|---|
| Runs migrations on boot | **No.** See §3 — this is the one thing to read before deploying. |
| Runs as root | No. UID 1001, `/app` not writable by the service account. |
| Contains secrets | No. `.dockerignore` excludes `.env*` except the examples. |
| Contains a compiler | No. Two-stage build; the runtime stage has only the venv. |
| Serves `/health` | Yes — liveness only, deliberately does not touch Postgres. |
| Serves `/health/ready` | Yes — 503 if Postgres is unreachable. Use for deploy gates. |

---

## 1. Prerequisites

- Docker with BuildKit.
- AWS CLI v2, credentials for an IAM principal that may push to ECR, register task definitions and
  run tasks.
- A VPC with at least two private subnets in different AZs, a public subnet for the ALB, and a NAT
  route or VPC endpoints for the private subnets. **Fargate tasks in private subnets need egress to
  Supabase, Groq, Gemini, AssemblyAI and Deepgram.** Without NAT they will boot and every outbound
  call will time out — the app looks alive and nothing works.
- A domain or subdomain for the API, e.g. `api.your-domain.com`.

---

## 2. Build and push

```bash
cd backend

export AWS_REGION=eu-west-1
export ACCOUNT_ID=123456789012

# One-time.
aws ecr get-login-password | docker login --username AWS --password-stdin \
    "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
aws ecr create-repository --repository-name memoir-backend

# Build, then push. `:latest` is what the task definition references; for a real
# release, tag with the git SHA and update the definition, so a running task can
# always be traced to a commit.
docker build -t "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/memoir-backend:latest" .
docker push "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/memoir-backend:latest"
```

Smoke-test the image locally before it goes near AWS:

```bash
cp .env.production.example .env.production   # fill it in
docker compose build
docker compose up api
curl -fsS http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/health/ready    # 200 if Postgres is reachable
```

The container refuses to start if the AI provider chain is empty, if `COOKIE_SAMESITE=none` is set
without `COOKIE_SECURE=true`, or if `CORS_ORIGINS` is `*`. Those are boot-time failures by design —
each one is a configuration mistake that otherwise produces silent, total auth failure in
production.

---

## 3. Migrations — read this before your first deploy

**The container does not migrate on startup, and neither should any deploy script.** This is not
caution for its own sake. Three revisions in the chain are not idempotent:

| Revision | What it does | Second run |
|---|---|---|
| `879e2c8d1b8c` | `ADD CONSTRAINT` | fails: already exists |
| `080151e0f1e8` | `DROP CONSTRAINT` | fails: does not exist |
| `cd3760273110` | `DROP COLUMN` + `ADD COLUMN` | **succeeds, and erases every organize run** |

`cd3760273110` drops and re-adds the `organization_status` columns on `memoir`. If the database's
`alembic_version` is at or behind that revision, a blind `alembic upgrade head` does not error — it
succeeds and silently empties those columns. Running that inside a task entrypoint turns a routine
deploy into data loss with no rollback, triggered by the deploy you were doing to fix something else.

The two revisions that are new (`c3f9a1b204d7`, `e7a41c5b9f32`) are pure `CREATE TABLE` on tables
that do not exist yet. Advancing from `d8ebd28b0d20` — the revision immediately before them — is
therefore safe.

### 3.1 Step 0: find out where you actually are

```bash
docker compose run --rm migrate-check
```

```
database revision: <whatever>
not at head: <whatever> (expected e7a41c5b9f32)
```

Three possible outcomes:

**`e7a41c5b9f32` (head)** — nothing to do. Deploy the new image.

**`d8ebd28b0d20`** — the expected case for this release. Run the migration (§3.2).

**Anything else, or no `alembic_version` table at all** — stop.

An absent table means the database was created from `migrations/0000_bootstrap_sandbox_schema.sql`
rather than by Alembic, which is the intended path for an existing Supabase project. Stamp it, but
only after confirming the schema it produced matches the revision you are stamping to:

```bash
# Inspect before you stamp. If organization_status exists on memoir, the schema
# is NOT the shape cd3760273110 expects and running that revision will drop it.
docker compose run --rm api python -c "
import psycopg
from src.core.config import settings
with psycopg.connect(settings.database_url) as c:
    print(c.execute(\"\"\"
        select table_name, column_name from information_schema.columns
        where table_schema='public'
          and table_name in ('memoir','organization_job_run','organization_proposal')
        order by table_name, column_name
    \"\"\").fetchall())
"
```

Then `alembic stamp d8ebd28b0d20` **only** if the columns above are already in their final shape.

**Never** resolve an unexpected revision with `--force` on the basis of this document. Inspect the
database, then decide. The runner's `--force` exists for an operator who has done that inspection.

### 3.2 Step 1: migrate, deliberately

```bash
docker compose run --rm migrate
```

It reads `alembic_version`, logs the revision it found, and advances only from a revision it can
prove is safe. Read what it prints. Then confirm:

```bash
docker compose run --rm migrate-check   # exit 0
```

On ECS, as a one-off task:

```bash
aws ecs run-task \
  --cluster memoir \
  --launch-type FARGATE \
  --task-definition memoir-backend \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","scripts/migrate.py"]}]}' \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-a,subnet-b],securityGroups=[sg-tasks]}"
```

Wait for it to reach `STOPPED` with exit code 0 **before** updating the service. A service rolling
out against a database the migration has not reached will 500 on the first query against a table it
does not have.

### 3.3 Ordering rule for every future release

```
1. build + push image
2. run migrate as a one-off task, wait for exit 0
3. update the service to the new task definition
```

Reversing steps 2 and 3 is what breaks. The application is expected to tolerate the *old* schema,
because the migration only ever adds tables.

---

## 4. ALB

Target group settings that matter for this service:

| Setting | Value | Why |
|---|---|---|
| Health check path | `/health` | Liveness. **Not** `/health/ready` — see §6. |
| Health check interval | 30s | |
| Matcher | HTTP 200 | |
| Deregistration delay | 60s | Longer than `stopTimeout` (45s) so in-flight requests drain instead of being cut. |
| Idle timeout | 120s | Must exceed the container's `--timeout-keep-alive 65`. |
| Stickiness | off | Stateless API. The in-process rate limiter is per-task regardless; affinity would make that worse, not better. |

Route `api.your-domain.com/*` → target group. Terminate TLS with an ACM certificate. Enable HTTP→HTTPS
redirect. Do not enable WAF on `/organize` without checking the body-size limit — audio uploads go
through `/api/media`, not the AI endpoints.

Security groups: ALB ingress 443 from `0.0.0.0/0`; task ingress 8000 from the ALB's security group
**only**. The task SG needs egress to the internet (Supabase, the AI providers) — see §1.

---

## 5. IAM

Two roles, and the distinction matters:

**`memoir-backend-execution`** (assumed by ECS on your behalf) needs:

```
ecr:BatchGetImage, ecr:GetDownloadUrlForLayer, ecr:GetAuthorizationToken
logs:CreateLogStream, logs:PutLogEvents
secretsmanager:GetSecretValue          # on memoir/* only
ssm:GetParameters                     # if any secret is an SSM parameter
```

**`memoir-backend-task`** (assumed by the running container) needs **no AWS API access at all**. The
backend talks to Supabase, not to AWS. Grant it nothing. If a future feature needs S3, add the
narrowest policy then — not now, not "for convenience".

---

## 6. Health checks: which one goes where

This trips people up, and getting it wrong turns a 30-second database blip into a full outage.

- **`/health`** — does not touch Postgres. Use this as the ALB health check and as the container
  health check (inherited from the image). If this probe checked the database, a database blip would
  mark every task unhealthy simultaneously, ECS would replace all of them, and the replacements
  would need the same unavailable database. That is a replacement storm that cannot help.
- **`/health/ready`** — 503 if Postgres is unreachable. Use as a **deploy gate** (do not promote an
  image whose readiness fails) and as a CloudWatch alarm on database health. Do not wire it to the
  ALB.

---

## 7. Vercel frontend

The frontend needs to know where the backend is, and the backend needs to know where the frontend
is. Both halves are required; a mismatch produces a 401 that looks like a backend bug.

| Where | Variable | Value |
|---|---|---|
| Vercel | `NEXT_PUBLIC_API_BASE_URL` | `https://api.your-domain.com` |
| Vercel | `NEXT_PUBLIC_API_TIMEOUT_MS` *(optional)* | `10000` |
| ECS | `CORS_ORIGINS` | the Vercel origin, **no trailing slash** |
| ECS | `SHARE_LINK_BASE_URL` | `https://your-vercel-domain.vercel.app/share` |

`NEXT_PUBLIC_API_BASE_URL` is the only `NEXT_PUBLIC_` variable the deployed app needs.

There is no `NEXT_PUBLIC_API_URL` and no `NEXT_PUBLIC_SITE_URL`; an earlier revision of this table
listed both, and setting either achieves nothing. `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_ANON_KEY` do appear in `frontend/src/lib/config/env.ts`, but they are
declared optional and nothing imports them — auth is entirely backend-issued. Do not set them on
Vercel unless a future feature adds direct client-side Supabase access.

These names were read out of `frontend/src`, not inferred. Note that they point in opposite
directions, which is the part that is easy to get backwards: `NEXT_PUBLIC_API_BASE_URL` names the
**backend**, and `CORS_ORIGINS` names the **frontend**. Setting only one half is the failure this
section exists to prevent.

**`NEXT_PUBLIC_*` is inlined at build time.** Next.js replaces every reference to it in the client
bundle with a literal value during `next build`, so the deployed app does not read the variable at
runtime. Adding or changing one on Vercel requires a **redeploy** to take effect; editing the
variable alone changes nothing.

**It is currently unenforced.** `frontend/src/lib/config/env.ts` validates this variable with Zod
and throws at boot if it is missing or malformed — but no module imports `env.ts`, including
`src/lib/api/client.ts`, which reads `process.env` directly and falls back to
`http://localhost:8000`. So in practice a forgotten variable does not fail loudly: every user's
browser quietly tries to call their own machine, and it surfaces as a generic network error rather
than a CORS one. Treat the variable as required and verify it explicitly.

The cross-domain cookie is the thing that breaks. Vercel and AWS are different registrable domains,
so the auth cookie must be `SameSite=None; Secure`. `COOKIE_SAMESITE=none` without
`COOKIE_SECURE=true` is refused at boot; browsers discard such a cookie outright, login appears to
succeed, and every subsequent request is unauthenticated.

---

## 8. Post-deploy verification

```bash
# 1. Liveness and readiness.
curl -fsS https://api.your-domain.com/health
curl -i    https://api.your-domain.com/health/ready

# 2. CORS preflight from the real frontend origin. Must echo that origin, not "*".
curl -i -X OPTIONS https://api.your-domain.com/api/memoirs \
  -H "Origin: https://your-vercel-domain.vercel.app" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"

# 3. Unauthorized is 401, never 500 and never a stack trace.
curl -i https://api.your-domain.com/api/memoirs

# 4. Authorization denial is 404, not 403.
#    (Requires a token. Anything else here is a defect.)
```

Then, in the browser: log in, create a memoir, submit a memory, click Organize, watch the real
stage progress, review the proposal, confirm it, and confirm the chapters appeared in both the
timeline and the material view with the originals untouched.

---

## 9. Known gaps in this deployment

State these plainly rather than discovering them in an incident.

1. **Background jobs are in-process.** `BackgroundTasks` runs in the worker that received the
   request. An ECS deploy, a scale-in, or a task crash mid-pipeline loses that run; the read path
   presents it as `stalled`, which is correct but not resumable. `stopTimeout: 45` narrows the
   window; it does not close it. A multi-agent pipeline taking 30 s–3 min is exactly the wrong
   workload for an ephemeral process. This was a deliberate decision (see root `AGENTS.md` §5) and it
   is the single biggest reliability risk in the current architecture.

2. **The chat rate limiter is per-process.** Each worker keeps its own counters, so the effective
   limit is `WEB_CONCURRENCY ×` the configured value, and it resets on every deploy. In-process
   dicts are also the usual unbounded-memory leak. Not fixed.

3. **Nothing has run against a live database or a real provider.** The migrations have never been
   applied. `gemini-3.6-flash` is unverified as a real model id. This deployment is the first time
   any of it will execute.

4. **The AI organization frontend page does not exist.** The backend endpoints are deployable; the
   review-and-confirm UI that is the point of the propose → review → apply flow is not written. Do
   not demo Organize to anyone expecting a product.

5. **No log-based alerting is configured.** §6 says where to put it; nothing here creates the alarm.