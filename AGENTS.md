# AGENTS.md — Backend

Backend-specific agent instructions. Repo-wide invariants and the AI Organization feature spec
live in the **root `AGENTS.md`** — read it first; it is not repeated here.

Stack: FastAPI, Supabase (Postgres + Storage + Auth), Pydantic v2, OpenAI-compatible clients.

---

## 1. Layering

```
src/api/           routes: what can be requested, request/response shape
src/domain/        business rules: what is allowed to happen
src/integrations/  plumbing: Supabase, storage, AssemblyAI, Groq, Gemini, Deepgram
src/schemas/       Pydantic contracts
src/core/          auth + config. Depends on nothing.
```

**Dependencies point one way: `api` → `domain` → `integrations` → `core`.**

`integrations/` never imports from `domain/` or `api/`. If a repository needs to know a business
rule, the rule is wrong or the layer boundary is wrong. Do not import upwards to "just fix one
thing" — that is how the boundary dies.

Two tests for whether code is in the right place:

- **Is it a rule or plumbing?** Rules go in `domain/`. Plumbing goes in `integrations/`.
- **Could this be reused by a background job or a different route?** Then it is not route logic.

One shared decision point beats N copies. `domain/access_control.py` answers "may this caller
access this memoir?" for every route. Copy-pasted permission logic drifts, and the drift is a
security bug that ships.

---

## 2. Routes

- Business logic belongs in `domain/`, not in the handler.
- Every route that touches a memoir resolves ownership **server-side** from the authenticated
  user. A `memoir_id` in the path or body is a hint, never authorization.
- `Depends(get_current_user)` returns a **dict**, not a string. Annotate it as `dict`. Do not
  "fix" this by stringifying at the call site — resolve the id once, explicitly.
- Register literal-path routes (`/chapters/reorder`) **before** parameterized ones
  (`/chapters/{chapter_id}`), or the literal path is swallowed.
- On denial return **404**, not 403.
- 4xx for caller errors, 5xx for our failures. Do not report a database outage as a 400.

---

## 3. Schemas

- **Every** request schema sets `model_config = ConfigDict(extra="forbid")`.
- Never `Dict[str, Any]` on a request body that reaches a database write. An unvalidated dict on a
  write path is the single highest-severity bug class in this codebase — it has happened.
- Bound everything: `max_length` on strings, `min_length`/`max_length` on lists, `ge`/`le` on
  numbers. An unbounded list field is a free DoS and a free unbounded DB write.
- Constrain values that land in a Postgres **enum** column with `Literal`, not `str`. A free-form
  string reaches the database and surfaces as a 500 instead of a 422. Keep the literal values in
  sync with the enum definition in `src/db/models/_enums.py`.
- LLM response schemas are contracts with the model. They carry **no field the model could use to
  write or invent narrative text** unless that is the explicit purpose of the field. If the model
  should only move things around, its schema has ids and nothing else.

### 3.1 Strict-mode schema generation

Groq's Structured Outputs support a `strict` mode (constrained decoding) that requires every field
to be `required` and every object to set `additionalProperties: false`. Pydantic models with
`Optional` fields carrying defaults do not satisfy that and are rejected with a 400 at request time.

Do not contort the Python-side models to fit. `StrictSchemaModel` in `schemas/organization.py`
supplies a `strict_json_schema()` classmethod that reshapes the schema: optional fields become
nullable-and-required, defaults and titles are stripped, `$defs` preserved.

Two traps in that reshaping, both hit already:
- Pydantic emits its own `required` key (only the non-defaulted fields). It must be **dropped**,
  or it overwrites the computed all-required list depending on key iteration order.
- `properties` values must be **recursed into**. Assigning the dict directly leaves nested `default`
  keys in place and the provider still rejects the schema.

Both failures surface as an opaque 400 that reads like a model problem.

---

## 4. Repositories

- **Scope every mutation by `memoir_id`.** `.eq("id", x)` alone is a cross-tenant write waiting
  for a caller bug. `supabase_admin` bypasses RLS, so this filter is the only thing standing
  between a mistake and a data breach.
- Read paths: select named columns, never `*`. `*` leaks storage paths and internal ids into
  responses that were never designed to carry them.
- Filter soft deletes (`.is_("deleted_at", "null")`) and status explicitly on every query. Do not
  rely on a caller having filtered already.
- Prefer batched writes (upsert / RPC) over per-row loops. An N+1 write loop inside a background
  job is a scalability cliff that only shows up under real data volume.
- Multi-step writes need a real rollback story. Snapshot first, and if you cannot make it atomic,
  say so in the docstring rather than implying atomicity.

---

## 5. LLM and AI integrations

- All provider access goes through `src/integrations/llm/` and `src/integrations/stt/`. No
  provider name appears anywhere else. Adding a provider must not require touching `domain/`.
- Use `AsyncOpenAI` in the AI paths. Never a bare module-level `httpx.post` — no pooling, no reuse,
  no timeout discipline.
- Provider capabilities are **per provider**, not sniffed from the base URL string. Checking
  `"groq" in base_url` to decide a request shape is a latent outage.
- Structured outputs: match the schema to the provider's mode. Strict mode rejects optional fields
  with defaults. Keep a strict-mode schema variant rather than disabling strictness.
- Catch specific exceptions. Never classify an error by substring-matching its message
  (`if "429" in str(e)`) — it produces both false positives and missed errors.
- Use `logger`, never `print`. Never log memory bodies, transcripts or API keys.
- Bound everything sent to a model: input size, output tokens, history length, call count.

**Treating model output as untrusted** — non-negotiable, and the thing most likely to be lost when
refactoring an AI feature:

1. Parse against a schema. Shape is guaranteed by the schema; **content is not**.
2. Validate every id against the exact set that was sent. An unknown id means the model was
   confused, so reject the **entire** response — never partially trust it.
3. Validate ordering, counts and bounds in code.
4. Re-verify ids belong to the target tenant at write time.
5. Fabricated content must never be persisted as if real. If a fallback is needed, produce nothing
   rather than plausible-sounding filler.

### 5.1 Failover policy — the distinction that matters

A provider is failed over on **infrastructure** failure: timeout, 429, 5xx, connection error,
empty `choices`, null content.

A provider is **NOT** failed over because a well-formed response failed *content validation*. That
is a bug or a confused model, not an outage. Retrying it elsewhere hides the defect behind a coin
flip and spends money every time it happens. It propagates immediately.

Classify by exception **type and status code**, never by substring-matching the message
(`if "429" in str(e)` misses real 429s and matches unrelated errors). Note that a 429 arriving as a
plain `APIStatusError` rather than `RateLimitError` — which happens behind a proxy — must still
fail over.

### 5.2 Prompt injection from stored content

Chapter titles and summaries are AI-authored and then owner-edited text. An owner's request is
also free text. All three can contain something that reads like an instruction.

Fence and label every interpolated block, and state in the system prompt which block is the only
one treated as a request. See `REFINER_SYSTEM_PROMPT` in `domain/organization_service.py`.

This is not theoretical defence-in-depth: the proposal payload is server-held precisely so a
client cannot inject arbitrary structure, and the same reasoning applies to text the server
itself stores.

---

## 6. Background jobs

FastAPI `BackgroundTasks`, not Celery (deliberately removed — see commit `c100a07`; `celery`
remains in `requirements.txt` as an unused leftover and should be dropped).

Before writing one, answer: **what if it runs twice, dies halfway, or runs on two servers?**

- Idempotent by construction. Check current state before acting.
- Every exit path resolves the job to a terminal state. Nothing upstream awaits the return value.
- Bounded attempts. On cap, stop and record it.
- Present a stuck `running` as `stalled` at read time, derived from elapsed time. There is no
  heartbeat and no crash recovery, so the read path must be smarter than the write path.
- Persist per-stage progress for multi-stage pipelines.

**The asyncio trap.** A sync background callable runs in Starlette's threadpool. An `async def` one
is awaited **on the event loop** and will block every concurrent request for its entire runtime.
Keep the entrypoint sync and hand off explicitly. See root `AGENTS.md` §5.

---

## 7. Config

- One `Settings` class in `src/core/config.py`. Required secrets have **no default** and fail at
  import so the process refuses to boot.
- Optional providers (fallbacks) are `Optional[...] = None`, plus a startup validation that refuses
  to boot when the *active chain* has zero usable providers. A missing fallback must fail loudly,
  not silently degrade.
- No bare `os.getenv()` in application code, and no fallback chains like
  `settings.x or os.getenv("Y")` — that converts a clear config error into a confusing 401 at
  runtime.
- Never log or echo a secret.

---

## 8. Before calling backend work done

- [ ] Dependencies point one way; no upward imports.
- [ ] Every request schema `extra="forbid"` and fully bounded.
- [ ] Every mutation scoped by `memoir_id`.
- [ ] Denials return 404; 4xx/5xx correct.
- [ ] LLM output validated in code; no fabricated content persisted; `memory.body_text` untouched.
- [ ] Background job idempotent, terminal-state on every path, sync entrypoint.
- [ ] No `print()`; `logger` only, no user content logged.
- [ ] No unbounded loops, dicts, prompts or list fields.
- [ ] Every external call has a timeout.
- [ ] Schema changes made as an Alembic revision, never a raw SQL file; models and migration in
      agreement (`python _ddl_parity_check.py`).