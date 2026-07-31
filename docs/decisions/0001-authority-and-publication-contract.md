# ADR 0001: Authority and immutable publication contract

- Status: Accepted
- Date: 2026-07-30

## Decision

Use Git artifacts, LangGraph checkpoints, OpenProject, GitHub, and Windmill for
their distinct canonical responsibilities as listed in `AGENTS.md`. The planner
emits proposals only.

Publication identity is `plan_id + node_key`; managed-field hashes provide
change detection. Windmill creates an immutable publication envelope after the
planning PR merges. That envelope binds the merge commit, `backlog.yaml` blob
SHA, canonical SHA-256, approval event, and OpenProject snapshot.

`plan.approved_planning_commit` is intentionally nullable in the proposal
schema. A file in a Git commit cannot contain that same commit's hash. Apply
requires the external publication envelope and rejects an unapproved proposal.
After verifying the raw artifact hash and Git blob identity, the publisher
materializes that approved commit only in memory so the OpenProject Planning
commit field and managed hash bind the merge commit without changing the
approved artifact bytes.

## Consequences

- Repeated apply of the same envelope is a zero-operation replay.
- A stale OpenProject lock version, ETag, snapshot hash, commit, or artifact
  hash fails closed.
- Human-owned fields never appear in the managed hash.
- Title changes cannot fork identity.
- Audit records can reproduce every proposed and applied operation.
