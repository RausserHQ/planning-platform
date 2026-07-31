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
- human-owned fields before and after replan.

The run is passing only when the live sequence demonstrates interrupt/resume,
planning PR merge, journaled publication, correct hierarchy/relations,
implementation PR/check synchronization, one intentionally suppressed webhook
repaired by reconciliation, one bounded partial replan, and an unchanged
zero-operation replay.

Operational evidence also includes one pod restart per service, a database and
attachment restore into isolation, a deliberately invalid webhook, a stale
thread finding, alert delivery to a named human receiver, and application
upgrade/rollback. Configuration or mocked API output is not acceptance
evidence.
