# Architecture

## Authority and trust flow

```text
OpenProject Idea/comment
  -> signed webhook
  -> Windmill validation + delivery dedupe
  -> private planner API
  -> PostgreSQL-backed LangGraph interrupt/checkpoint
  -> typed proposal + planning artifacts
  -> Windmill GitHub adapter
  -> planning PR approval
  -> immutable publication envelope
  -> validate + dry-run + stale-base gate
  -> OpenProject publisher
  -> audit record

GitHub PR/check events
  -> Windmill signature/dedupe
  -> deterministic evidence/status policy
  -> OpenProject adapter

Nightly schedule
  -> snapshot GitHub + OpenProject + planner/Windmill state
  -> deterministic reconciler
  -> safe repairs or recommendations
```

The planner network identity accepts immutable repository context only from
the authenticated broker and has its own `planner` database role. It has no
direct repository credential, OpenProject token, GitHub credential, Windmill
token, or access to the OpenProject/Windmill databases.

Each mutating planner execution is fenced by a PostgreSQL session advisory
lock and a LangGraph saver bound to that same non-reconnecting connection.
Checkpoint reads use the application pool, but executions do not, preventing
long model calls from exhausting the read/idempotency pool.

## Durable identity

- Planning thread: `openproject:<idea-id>:planning:<plan-version>`
- Work package: `(plan_id, node_key)`
- Publication: `(plan_id, plan_version, approved_commit, backlog_sha256)`
- Event: `(source, source_delivery_id)`
- Side effect: caller-provided deterministic `idempotency_key`

Titles, comments, branch names, and OpenProject numeric IDs are never used as
cross-system identity.

## Replanning

A replan creates a higher plan version and supplies affected stable keys. The
diff updates only managed fields on those keys, adds new keys, and marks removed
keys Superseded. Status, assignee, comments, time entries, actual effort, PR
links, human notes, and runtime failures are outside the managed field set.

## Deployment

- OpenProject 17.6.0 through official chart 13.9.0.
- Windmill chart 4.0.223 with the official Windmill 1.775.2 CE image pinned by
  linux/amd64 digest and extended with the deterministic lifecycle package.
- Planner API built from this repository.
- One CloudNativePG cluster with separate `openproject`, `windmill`, and
  `planner` databases and login roles.
- S3-compatible OpenProject attachments and PostgreSQL backups use
  infrastructure-owned object storage.
