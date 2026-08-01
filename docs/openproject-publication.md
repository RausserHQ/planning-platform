# OpenProject publication

The HTTP adapter targets **OpenProject Community 17.6.0** and its API v3 form
workflow.  The Rails bootstrap is likewise pinned to 17.6.0 because it uses
version-sensitive internal models.  Do not run either against another release.

`openproject/publisher-config.example.yaml` is a shape-only example: replace
every numeric ID with IDs from the target instance after the pinned bootstrap
has verified types, statuses, and custom fields.  The adapter accepts IDs only
through `OpenProjectAdapterConfig`; no instance ID is compiled into it.

The publisher authenticates as Basic `apikey:<token>` and uses bounded HTTP
timeouts.  Give the token only to the Windmill publication worker.  Never put
it in this repository, a runner argument, a log, or a planning artifact.

## Deterministic CLI publication

Use a captured, immutable OpenProject snapshot for review-only output:

```bash
planning publish backlog.yaml \
  --dry-run \
  --against-openproject openproject-snapshot.json
```

Apply uses the same fail-closed publisher as the Windmill lifecycle worker. It
loads the exact backlog bytes, a non-secret instance-ID config, and the exact
merge-bound publication envelope; then it refreshes live OpenProject state
before each side effect and writes intent/outcome to the PostgreSQL journal.
The journal schema must already be migrated and Ready. The CLI never performs
schema setup implicitly.

```bash
export OPENPROJECT_API_TOKEN="$(read-token-from-approved-secret-source)"
export PLANNING_LIFECYCLE_DATABASE_URL="$(read-dsn-from-approved-secret-source)"
export PLANNING_PUBLICATION_POLICY=/operator-owned/publication-policy.yaml

planning publish backlog.yaml \
  --apply \
  --publisher-config openproject/publisher-config.yaml \
  --publication-envelope publication-envelope.yaml
```

The two file options may instead be provided as
`PLANNING_OPENPROJECT_PUBLISHER_CONFIG` and
`PLANNING_PUBLICATION_ENVELOPE`. Secrets have no command-line option: supply
them only through the worker's environment. `PLANNING_PUBLICATION_POLICY` is
mandatory and has no caller option. Provision it as operator-owned policy using
the shape in `openproject/publication-policy.example.yaml`; it pins the local
repository root, exact `origin` URL, trusted protected ref, repository-relative
backlog path, and canonical non-secret OpenProject target/config hash.

Generate that policy hash from the exact non-secret publisher config:

```bash
planning publication-target \
  --publisher-config openproject/publisher-config.yaml
```

Copy the single 64-character result into `openproject_target_sha256`. Re-run
the command after any URL, project, ID mapping, timeout, or collection-bound
change; an in-progress publication must be resolved under its original target
instead of updating policy mid-retry.

Before apply, refresh the configured trusted ref through the repository's
normal authenticated Git workflow. The CLI verifies that the approved commit
is an exact commit reachable from that ref, that the selected path is a regular
Git blob (not a symlink or submodule), and that its blob ID and bytes exactly
match both the local artifact and envelope. The OpenProject target hash is
derived from the loaded publisher config and must match policy; it is then
included in the durable journal envelope, so a retry cannot move to another
instance, project, ID map, or runtime bound.

The envelope file must contain exactly the fields shown in
`openproject/publication-envelope.example.yaml`, all bound to the approved
artifact, merge, source blob, snapshot, and stable publication identity.
`--against-openproject` is deliberately rejected with `--apply` because a
static snapshot cannot authorize a live mutation. Output `operation_count`
reports only adapter effects completed by that invocation; `resumed: true`
distinguishes a durable retry, and a terminal replay reports zero effects.

This local command is an explicit human fallback: repository/ref policy plus
the operator's `--apply` invocation authorizes it, but local Git reachability
does not independently prove GitHub review or required-check evidence. Normal
production publication must use the lifecycle path, which re-verifies the
merged PR, current human approval, required checks, immutable GitHub blob, and
durable approval record before calling the same publisher.

## Bootstrap

Mount the following pre-created secret files into the official 17.6.0
OpenProject container and run the Ruby script with `bundle exec rails runner`:

```text
OPENPROJECT_API_TOKEN_FILE=/run/secrets/openproject-publisher-token
OPENPROJECT_WEBHOOK_SECRET_FILE=/run/secrets/openproject-webhook-secret
PLANNING_PLATFORM_SERVICE_USER=planning-platform-publisher
PLANNING_PLATFORM_PROJECT=planning-platform
PLANNING_PLATFORM_ALERT_ASSIGNEE_LOGIN=admin
PLANNING_PLATFORM_WEBHOOK_URL=https://windmill.internal/api/w/planning/openproject
```

The token plaintext and webhook secret files are explicit preconditions. The
runner idempotently creates the private project and non-administrator service
user when absent, stores only the OpenProject API token hash, and never prints
the supplied token. It fails if a secret is absent/empty, the release differs,
a required type/status/custom field is duplicated or semantically
incompatible, or its expected workflow/webhook model is unavailable. Windmill
validates incoming OpenProject webhook signatures as
`X-OP-Signature: sha1=<HMAC-SHA1(raw body)>` using the separately injected
webhook secret.

The publisher role is project-scoped and only receives work-package creation,
editing, and relation permissions. Bootstrap idempotently enables OpenProject's
`work_package_tracking` module on both the private planning project and its Idea
template while preserving unrelated enabled modules; role permissions remain
inactive without that project module. A separate alert-assignee role grants only
`view_work_packages` and passive `work_package_assigned` eligibility to the
pre-existing active human account named by
`PLANNING_PLATFORM_ALERT_ASSIGNEE_LOGIN`. Bootstrap removes that dedicated role
from a previous recipient during rotation. The publisher discovers exactly one
eligible User through the project-scoped `available_assignees` endpoint; zero
or multiple eligible users is a deployment error. The `Idea intake` template
asks for the problem, outcome, repository, constraints, and evidence. The
webhook subscribes to work-package create/update and comment events.

## Mutation rules

Planning-item identity is only `(Plan ID custom field, Node key custom field)`. The adapter
scans every page of the configured project and reads each matching candidate
before mutation; duplicates, inconsistent totals/offsets, changed collection
scope, pagination without progress, or configured page/item bounds stop
publication. Individual resources must provide an observed nonnegative integer
`lockVersion`; the adapter never invents a concurrency token. It validates a
create/edit form, then writes with the fresh `lockVersion`. One 409 causes
exactly one fresh identity, managed-content, and topology check and retry;
changed managed state or topology is a conflict. A lost or malformed create,
update, or relation response is never blindly retried: only an exact fresh
content-and-topology postcondition recovers it; otherwise the durable
publication intent remains ambiguous for an operator.

`Source requirements` is an OpenProject v17.6 `text` custom field. The adapter
writes its required `{raw: ...}` formattable shape, with the raw value encoded
as a canonical JSON string array. This is lossless even when one requirement
contains a newline. `Evidence state` is verified as `pending` on creation, then
is runtime-owned and is not part of later managed-content updates. Operational
alerts use the separate `Alert fingerprint` custom field as their stable
identity. Subject edits therefore cannot create duplicates; a missing identity
field or a collision with content lacking the exact generated alert marker
stops without mutation.

Only the region between the generated description markers is replaced. Status,
assignee, comments, time, actual effort, PR links, and other human fields are
not sent in updates. Removed nodes receive the configured `Superseded` status;
they are never deleted. If a later approved plan reintroduces a Superseded
identity, an explicit replay-safe operation moves it to `Ready`; an unchanged
subsequent reapply then has zero operations.

Operational alert creation assigns the new Task to the one bootstrapped human
alert assignee. Later alert updates never rewrite assignee or subject. They
converge only the generated description region, priority, and the alert-owned
Blocked/Done state with the current `lockVersion`.

Relations are scanned through global `/api/v3/relations` filters. Each managed
relation has one exact deterministic description marker. Human relations are
never deleted; a human relation with the same native semantic direction/type is
a hard collision. The projection is: `blocked_by` → reverse `blocks`,
`sequence_after` → `follows`, `related_to` → canonical sorted `relates`,
`decision_required` → `requires`, and `governed_by` → distinct-marker
`relates`. Every plan version shares a PostgreSQL advisory publication fence
with every other version of the same stable plan ID; distinct approval
deliveries therefore cannot publish two versions of one plan concurrently.
The explicit v5 journal migration refuses any unfinished pre-v4 publication
whose original operation order cannot be recovered and any unfinished v4
publication that predates target binding. It also refuses a terminal v4
publication while the matching lifecycle run is still `publishing`; inspect
the already-recorded effects and explicitly resolve that lifecycle run before
retrying the migration. Only terminal, fully applied legacy rows whose
lifecycle is already closed receive deterministic display ordinals, a sentinel
target, and an explicit archive identity; archived rows cannot be replayed.
New publications remain fully ordered, hash-verified, and bound to one
OpenProject target.
