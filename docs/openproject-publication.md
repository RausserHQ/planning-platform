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

## Bootstrap

Mount the following pre-created secret files into the official 17.6.0
OpenProject container and run the Ruby script with `bundle exec rails runner`:

```text
OPENPROJECT_API_TOKEN_FILE=/run/secrets/openproject-publisher-token
OPENPROJECT_WEBHOOK_SECRET_FILE=/run/secrets/openproject-webhook-secret
PLANNING_PLATFORM_SERVICE_USER=planning-platform-publisher
PLANNING_PLATFORM_PROJECT=planning-platform
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
editing, and relation permissions. The `Idea intake` template asks for the
problem, outcome, repository, constraints, and evidence. The webhook subscribes
to work-package create/update and comment events.

## Mutation rules

Identity is only `(Plan ID custom field, Node key custom field)`. The adapter
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
is runtime-owned and is not part of later managed-content updates.

Only the region between the generated description markers is replaced. Status,
assignee, comments, time, actual effort, PR links, and other human fields are
not sent in updates. Removed nodes receive the configured `Superseded` status;
they are never deleted. If a later approved plan reintroduces a Superseded
identity, an explicit replay-safe operation moves it to `Ready`; an unchanged
subsequent reapply then has zero operations.

Relations are scanned through global `/api/v3/relations` filters. Each managed
relation has one exact deterministic description marker. Human relations are
never deleted; a human relation with the same native semantic direction/type is
a hard collision. The projection is: `blocked_by` → reverse `blocks`,
`sequence_after` → `follows`, `related_to` → canonical sorted `relates`,
`decision_required` → `requires`, and `governed_by` → distinct-marker
`relates`. Every plan version shares a PostgreSQL advisory publication fence
with every other version of the same stable plan ID; distinct approval
deliveries therefore cannot publish two versions of one plan concurrently.
The explicit v4 journal migration refuses any unfinished legacy publication
because its original operation order cannot be recovered. Only terminal,
fully-applied legacy rows receive deterministic display ordinals and an
explicit archive identity; archived rows cannot be replayed. New publications
remain fully ordered and hash-verified.
