# Acceptance evidence protocol

Production acceptance uses three real OpenProject Ideas:

1. a single-repository feature producing 8–15 managed work packages;
2. a cross-repository initiative with an explicit integration story and typed
   upstream blocker;
3. a partial replan after human notes, assignment, linked PR evidence, and
   completed children exist.

For every pilot, record only identifiers and hashes:

- Idea ID, plan ID/version, planner thread ID, and trace ID;
- source repository commits;
- planning PR URL, head/merge commits, `backlog.yaml` blob SHA-1 and SHA-256;
- OpenProject IDs by stable node key and relation counts;
- Windmill job/delivery IDs;
- first-publication operation counts and replay operation count;
- immutable convergence-proof plan ID/version, Windmill job/delivery and trace
  IDs, and the audited `zero_operations` result;
- human-owned fields before and after replan.

The run is passing only when the live sequence demonstrates interrupt/resume,
planning PR merge, journaled publication, correct hierarchy/relations,
implementation PR/check synchronization, one intentionally suppressed webhook
repaired by reconciliation, one bounded partial replan, and an unchanged
zero-operation replay.

For the final published version of each pilot, manually run the operator-only
Git-synced `convergence_check` Windmill flow with that exact plan ID/version.
The acceptance record must show its immutable Git binding, audit record, and
`zero_operations`. A `drift_operations:<n>` result is non-success evidence and
must not be repaired through this proof flow.

Operational evidence also includes one pod restart per service, a database and
attachment restore into isolation, a deliberately invalid webhook, a stale
thread finding, alert delivery to a named human receiver, and application
upgrade/rollback. Configuration or mocked API output is not acceptance
evidence.
