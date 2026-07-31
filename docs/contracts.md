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

The publisher and replanner preserve status, assignee, comments, time entries,
actual effort, PR links, human notes, and runtime failure information unless
an approved migration explicitly names them. The lifecycle projection has one
narrow exception: configured implementation evidence may monotonically
advance `Proposed`/`Ready` to `In Progress` and then `Review`. It never moves a
status backward and never overwrites `Blocked`, `Needs Input`, `Done`,
`Superseded`, or `Rejected`.

Operational alert Tasks are a separate bounded projection: `Alert fingerprint`
is their stable identity, their generated region, priority, and Blocked/Done
state are automation-owned, and the initial assignee is the one bootstrapped
human alert recipient. Subject and assignee become human-owned immediately
after creation.

## GitHub lifecycle evidence

All planning pull requests are hosted in the configured
`PLANNING_ARTIFACT_REPOSITORY`, irrespective of which repositories supplied
planning context. That repository owns the stable validator workflow. A
planning pull request may publish only after the GitHub API independently
confirms its stored head commit, merge commit, at least one current human
approval on that exact head, and the successful
`planning-backlog-validation` check from the trusted GitHub Actions App.
Review IDs, actors, commit bindings, check-run IDs, App identities, and head
SHAs remain in the hashed evidence. The evidence digest and merge commit are
durably bound before the first publication read or mutation; a webhook payload
is only a trigger. An upgraded in-flight publication with no evidence digest
must pass the same read-back before it can resume.

Implementation pull requests use the immutable
`repository + pull_request_number` mapping in
`planning_lifecycle.implementation_pr_associations`. The mapped plan ID, node
key, work-package ID, and URL cannot be retargeted. Head observations are
monotonic, open/closed state is durable, successful checks bind to the current
head, and a merge commit is sticky. A new mapping is accepted only when the
node belongs to the newest published plan version, its managed repository
matches the webhook repository, and it is not terminal. Check advancement
requires the exact repository-specific names in
`PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON`, from the trusted GitHub
Actions App, on the current head. All PRs for one work package are aggregated
before one monotonic projection; closed-unmerged PRs are not active evidence.
Green evidence advances to `Review` without setting `Done`; failed checks
never invent a dependency blocker.

OpenProject comments written by this service always contain a deterministic
`planning-platform:comment` marker for idempotency. Markers are not
authorization. Signed webhook ingress reads the service account's immutable
ID from OpenProject `/api/v3/users/me`; only that actor is classified as a
service. A human may quote a marker and still provide planning input, while an
unmarked service comment can never resume an interrupt.

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
An authenticated internal publisher may read exact, hash-verified artifact
bytes from `GET /v1/plans/{thread_id}/artifacts`; this read-only handoff does
not transfer publication authority to the planner.
