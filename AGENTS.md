# Planning Platform Agent Policy

This repository is the open-source application half of the Idea-to-Tickets
platform. Kubernetes production desired state remains in
`RausserHQ/homelab-platform`.

## Authority boundaries

- Versioned planning artifacts own problem, requirements, architecture, and decisions.
- LangGraph/PostgreSQL owns in-progress planning checkpoints only.
- OpenProject owns ticket hierarchy, relations, status, assignee, and effort.
- GitHub owns source, planning artifacts, pull requests, reviews, checks, and evidence.
- Windmill owns external side effects, retries, schedules, and deduplication.
- Planner output is always a proposal. Planner code must not receive GitHub or
  OpenProject mutation credentials.

## Development rules

- Keep `packages/backlog-schema/schema/backlog.schema.json`,
  `packages/backlog-schema/schema/event-envelope.schema.json`, and
  `docs/api/planner.openapi.yaml` backward compatible within schema version 1.
- Stable publication identity is `plan_id + node_key`; never title matching.
- Apply operations must be preceded by validation, immutable-commit binding,
  and a stale-snapshot check.
- Preserve human-owned OpenProject fields on replan.
- Removed plan nodes are superseded, never deleted.
- All mutation paths require deterministic idempotency keys and audit records.
- Never log tokens, webhook secrets, private keys, repository content, or raw
  secret-bearing payloads.

## Required checks

```bash
python -m pytest
python -m ruff check .
python -m mypy src
planning validate evals/fixtures/single-repository/backlog.yaml
```
