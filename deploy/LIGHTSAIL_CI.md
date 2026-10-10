# CI/CD: pushing to `main` deploys to the live Lightsail service

`.github/workflows/deploy.yml` runs on every push to `main`: tests, then a
guarded migration, then build + push + deploy to the **existing** Lightsail
container service (this assumes the service was already created manually, as
it has been). There is deliberately no separate staging approval step — a
push to `main` that passes tests goes live.

## One-time GitHub setup

Add these under the repo's **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | An IAM user/role with the permissions below. Not the root account. |
| `AWS_REGION` | The region the Lightsail service actually runs in, e.g. `us-east-1`. |
| `LIGHTSAIL_SERVICE_NAME` | The exact name of the already-running Lightsail container service. |
| `ENV_PRODUCTION` | The **entire contents** of a filled-in `.env.production` (see `.env.production.example` in this directory's parent) pasted as one multi-line secret. |

`ENV_PRODUCTION` is one secret instead of twenty-plus because every value in
it becomes both (a) this pipeline's test/migration environment and (b) the
actual environment variables the deployed container runs with — one source
avoids them drifting apart. Paste it exactly in `KEY=value` lines; comment
lines (`#...`) and blank lines are stripped automatically before use.

**Do not reuse a staging/sandbox `READER_JWT_SECRET` or Supabase project
here.** This file's contents become the live backend's real configuration.

## IAM permissions the deploy user/role needs

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lightsail:PushContainerImage",
        "lightsail:CreateContainerServiceDeployment",
        "lightsail:GetContainerServices",
        "lightsail:GetContainerServiceDeployments",
        "lightsail:GetContainerImages"
      ],
      "Resource": "*"
    }
  ]
}
```

Lightsail's container APIs don't support resource-level scoping finer than
this. This user needs no other AWS permissions — it never touches ECR, EC2,
or anything outside Lightsail.

## What the pipeline does NOT do

- **It does not create the Lightsail service.** `LIGHTSAIL_SERVICE_NAME` must
  already exist (it does). Creating one is a one-time console/CLI action with
  its own capacity/pricing choice, deliberately not something a push to
  `main` should ever trigger by accident.
- **It does not run `alembic upgrade head` blindly.** It runs
  `python scripts/migrate.py`, the guarded runner already in this repo (see
  `deploy/README.md` §3), which refuses to advance from a revision it can't
  prove is safe. If the database is ever in a state that script won't touch
  automatically, the `migrate` job fails the whole pipeline rather than
  guessing — fix the database by hand (per `deploy/README.md`), then re-run
  the workflow.
- **It does not roll back on a failed health check.** Lightsail keeps serving
  the previous deployment's traffic while a new one boots, so a failure in
  the `deploy` job's wait step means the new version never came up and the
  old one is still live — not an outage, but also not a self-healing rollback.
  Investigate via `aws lightsail get-container-log` before pushing again.

## First run on an already-live service

The very first time this workflow runs against the service that's currently
running a manually-deployed image, treat it as a real deploy: watch the
Actions log, and have the manual deploy process on hand in case the pipeline
needs to be aborted. After that, it's routine.
