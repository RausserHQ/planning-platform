# Frozen v1 contracts

## Backlog semantics

- `parent`: work-breakdown hierarchy.
- `blocked_by`: hard semantic prerequisite.
- `sequence_after`: preferred order only.
- `related_to`: contextual relationship only.
- `decision_required`: unresolved choice represented by a Decision item.
- `decisions`: resolved Decision items that govern this item.
- `mutex`: runtime concurrency group and never an OpenProject blocker.

Every executable item traces to at least one source requirement or maintenance
objective. Every acceptance criterion pairs the desired state with an
observable way to prove it. Every result predicate uses a machine-checkable
kind.

## OpenProject managed fields

The publisher may manage title, generated description section, parent,
planning relations, initial priority, estimate, risk, repository, source
requirements, acceptance criteria, plan ID, node key, plan version, managed
hash, agent eligibility, and the approved planning commit. Evidence state is
initialized to `pending` at creation, then becomes lifecycle-owned and is never
reset by a replan.

It preserves status, assignee, comments, time entries, actual effort, PR links,
human notes, and runtime failure information unless an approved migration
explicitly names them.

## Publication operation vocabulary

`create_work_package`, `update_managed_fields`, `set_parent`,
`create_relation`, `remove_managed_relation`, `mark_superseded`, and
`record_audit`. Each operation carries `operation_id`, stable identity,
preconditions, managed before/after hashes, and trace ID.

Relation operation payloads retain canonical planning names (`blocked_by`,
`sequence_after`, `related_to`, `decision_required`, and `governed_by`) and
stable target identities. The OpenProject adapter owns the explicit projection
to native directional relations; core planning code never guesses numeric IDs
or emits OpenProject-only relation names.

## Planner API

The API accepts immutable snapshots and produces proposals. It cannot fetch or
mutate OpenProject or create GitHub branches/PRs. Windmill owns those effects.
