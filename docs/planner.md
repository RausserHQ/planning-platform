# Private planner

The planner is a private FastAPI service. It receives immutable repository
snapshots in `POST /v1/plans`; it never fetches repository or OpenProject data
and has no mutation clients or credentials.

An authenticated upstream broker is responsible for authorization and for
fetching each repository at the named immutable commit. The planner cannot
prove repository ownership or fetch provenance. It verifies every file hash,
the canonical repository snapshot digest, and the canonical Idea/OpenProject
binding digest before accepting the request. All timestamps in the intake and
resume contracts must include a UTC offset.

Repository snapshot digests are SHA-256 over canonical compact JSON with
sorted keys containing `name`, `commit`, and path-sorted
`{"path", "sha256"}` entries. `idea_sha256` is SHA-256 over canonical compact
JSON with sorted keys containing the exact validated `idea` and
`openproject_snapshot` objects.

Raw file content is passed to LangGraph as ephemeral runtime context and is
never stored in graph state. Checkpoints retain repository names, commits,
file paths, hashes, and sanitized model-derived material only. Sanitization
rejects common credential shapes, PEM blocks, and high-entropy token-like
values before any model result reaches a checkpoint. Idea title/description
and resume answers are sanitized before the initial invocation or resume
command, so their raw values never enter checkpoint history.

Every `/v1` operation requires
`X-Planning-Internal-Token: <PLANNER_INTERNAL_TOKEN>`. The shared dependency
compares the supplied credential in constant time. This protects model-budget
and snapshot-ingestion operations as well as artifact reads. Health and metrics
remain unauthenticated for cluster probes and scraping.

The planner accepts at most 4 MiB of validated UTF-8 request/context JSON,
independent of the broker's larger transport limit. Repository file paths must
be unique within each snapshot.

## Persistence

Production keeps one application-lifespan `AsyncConnectionPool` for read-only
checkpoint access and the planner-owned idempotency repository. Every
start/resume execution instead opens a fresh, concurrency-bounded
`AsyncConnection`, takes the thread's session advisory lock, and constructs
that execution's `AsyncPostgresSaver` on the exact same connection. The saver
cannot reconnect. The connection—and therefore the lock—is held through
idempotency finalize and then closed, with close awaited to completion even
under cancellation. Execution connections never consume the read pool, so
active model calls cannot starve claim/finalize or health traffic. Startup does
not mutate the database schema.

Run migrations explicitly before rollout:

```bash
PLANNER_DATABASE_URL=postgresql://... planning-planner-migrate
```

The migration invokes LangGraph's `AsyncPostgresSaver.setup()` and creates the
planner idempotency, resume-binding, and migration-marker tables. Readiness
requires the planner v2 migration marker and the exact LangGraph checkpoint
schema shipped by `langgraph-checkpoint-postgres` 3.1.1: the ordered migration
set `v0..v9`, every column's order, PostgreSQL UDT type, nullability, and
required default, plus the exact primary-key columns for all four checkpoint
tables. The same exact attestation covers the three planner-owned migration,
idempotency, and resume-binding tables and requires the planner v2 marker.

Idempotency uses short claim and finalize transactions. Model execution never
runs inside a database transaction. A live claim is renewed at a fixed
lease-duration interval; PostgreSQL alone determines expiry, so process clock
skew cannot trigger renewal timing. Concurrent duplicates receive a typed
`409`. Once a lease expires, the exact same operation kind, thread, and request
body may recover from durable LangGraph state. Resume recovery also verifies
the exact interrupt/comment binding and never consumes the same answer twice.
A thread may have only one unfinished mutation: a different idempotency key
fails closed, even after expiry, while only the owning key may recover.
Cancellation stops and awaits graph execution before the execution connection
is closed.

Each pending interrupt stores its aware creation timestamp plus a concise
model-derived explanation of why the answer changes the plan. A resume comment
must be strictly newer than that interrupt. The validated resume event trace ID
becomes the thread's current durable trace ID and is returned by the completed
response and every idempotent replay. Final relation output may reference only
requirements declared by the requirements pass, and every declared requirement
must remain covered; maintenance objectives are validated separately.

## Artifact handoff

When a plan reaches `artifacts_ready`, an authenticated internal publisher can
retrieve the exact checkpointed artifact bytes with:

```text
GET /v1/plans/{thread_id}/artifacts
X-Planning-Internal-Token: <PLANNER_INTERNAL_TOKEN>
```

The response includes each path, content, and SHA-256. The service recomputes
and checks every hash against the durable manifest before returning bytes.
Missing or invalid credentials return `401`; an unknown thread returns `404`;
an incomplete plan returns `409`.

## Runtime

Required environment:

- `PLANNER_DATABASE_URL`
- `PLANNER_OPENAI_MODEL`
- `OPENAI_API_KEY`
- `PLANNER_INTERNAL_TOKEN`

Stable threads are derived as
`openproject:<idea-work-package-id>:planning:<plan-version>`. Clients cannot
provide a thread ID on start.
