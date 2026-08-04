# Operator runbook

Production state is reconciled from `RausserHQ/homelab-platform`; this
repository supplies application images, OpenProject bootstrap logic, and the
Git-synced Windmill workspace. Do not make durable configuration changes in a
pod.

## Deployment order

1. Reconcile the CloudNativePG roles/databases and SOPS secrets.
   `PLANNING_RECOVERY_KEY_B64` must be canonical base64 for exactly 32 random
   bytes and must be mounted only in Windmill workers and lifecycle migration
   Jobs.
2. Run `planning-planner-migrate` against the planner database.
3. Run `planning-lifecycle-migrate` against the Windmill lifecycle database.
   This creates the delivery, correlation, audit, and publication-journal
   schemas under one advisory migration fence.
4. Install OpenProject 17.6.0 and run
   `openproject/bootstrap/17.6.0/bootstrap.rb` once through the pinned seeder
   image.
5. Install Windmill CE 1.775.2 and sync `windmill/` with CLI 1.775.2.
6. Start planner and Windmill workers, then enable the signed webhook routes.
   The worker environment must set:
   - `OPENPROJECT_BASE_URL` to the cluster-internal OpenProject service URL and
     `OPENPROJECT_CANONICAL_ORIGIN` to its public HTTPS origin. OpenProject
     requests keep the internal transport route while sending the canonical
     `Host` and `X-Forwarded-Proto` headers required by Rails;
   - `PLANNING_ARTIFACT_REPOSITORY` to the one repository that contains the
     `planning-backlog-validation` workflow;
   - `PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON` to a non-empty JSON object
     mapping every implementation repository that may advance on checks to its
     non-empty array of trusted required-check names, for example
     `{"Acme/service":["implementation-tests"]}`;
   - `PLANNING_IMPLEMENTATION_STALE_HOURS` to the positive interval after
     which an In Progress item with only closed-unmerged PRs is reported;
   - `PLANNING_THREAD_STALE_SECONDS` to the positive interval after which the
     latest nonterminal plan run is reported as a stale planning thread; and
   - `PLANNER_HTTP_TIMEOUT_SECONDS` to a bounded value from 31 through 900
     seconds. The production value is 600 seconds so synchronous model-backed
     planning requests are not cut off by the shared GitHub client's 30-second
     default.
   OpenProject ingress resolves the service actor ID through
   `/api/v3/users/me` with the scoped publisher token; comment markers are
   never used as identity.
7. Apply the scoped Alertmanager route only after the authenticated
   `planning/alerts` trigger is synced. A firing alert must create a Blocked
   operational Task; a resolved delivery must converge the same fingerprint to
   Done while preserving human text outside the generated region. The created
   Task must be assigned to the one bootstrapped human alert recipient.

All migrations are explicit Jobs. Application startup is read-only with
respect to schema and reports not-ready when any required table is absent.

## Readiness checks

```bash
curl -fsS http://planner-api.planning-platform.svc:8080/health/ready
curl -fsS http://openproject.planning-platform.svc/api/v3
curl -fsS http://windmill.planning-platform.svc/api/version
```

Confirm the planner Deployment and OpenProject/Windmill HelmReleases are
Available/Ready, the three databases accept only their owning roles, the
nightly schedule is enabled, and both webhook triggers retain `raw_string:
true`.

## Windmill sync

Use an installation-scoped deploy token injected into the one-shot sync Job.
Preview before the first production import:

```bash
wmill sync push --dry-run
wmill sync push
```

The workspace is `planning`; Git owns scripts, flows, schedules, and triggers.
Secrets, users, groups, and settings are deliberately excluded from sync.
The custom worker image supplies this package through `PYTHONPATH` for ordinary
Python discovery and Windmill's `ADDITIONAL_PYTHON_PATHS`; it filters only the
local `planning-platform` dependency. The image replaces Windmill's inherited
root-home Bun link with the pinned npm-installed `wmill` executable under
`/usr/bin`, and the release gate executes it explicitly as UID/GID 1000.

## Immutable convergence proof

An operator may run the Git-synced `convergence_check` flow for one exact
published `plan_id` and positive `plan_version`. The flow derives its delivery
identity from the Windmill root job, reads the stored immutable bindings and
the exact GitHub artifact, snapshots OpenProject, and records a
`convergence_check` audit row. `zero_operations` is the only successful
acceptance result; `drift_operations:<n>` records the number of planned
mutations and performs no repair, publish, journal, or OpenProject write.
In the Windmill workspace, manually run the `convergence_check` flow and enter
only the published plan ID and version; never provide or reuse an approval
event. Windmill supplies the job identity used for the delivery key.
Record the plan identity, Windmill job/delivery identity, trace ID, and audit
outcome. Do not use this flow to approve, publish, or repair a plan.

## Recovery

- A retryable failure expires only its token-fenced lease. A heartbeat keeps a
  live owner from being reclaimed.
- A terminal failure is visible in Windmill and
  `planning_lifecycle.delivery_deduplications`.
- Run `dead_letter_recovery` only with the original trusted envelope, an
  operator identity, and a written reason. Recovery is audited and rotates the
  claim token. A failed recovery atomically returns the delivery to
  `dead_letter`, so a later authorized attempt remains possible.
- When a terminal comment delivery left `pending_resume_ciphertext` and an
  expired planner claim after consuming the interrupt, use the operator-only
  `abandon_terminal_resume` Windmill script. Supply the exact plan, thread,
  planner idempotency key, operator, and reason. The operation never accepts or
  replays an event payload. It proceeds only when the delivery is dead-lettered
  and unfenced, the sealed request belongs to that delivery, the planner has no
  artifact result, and the exact pre-resume interrupt checkpoint exists. It
  tombstones the old planner claim, restores that interrupt through the
  application graph, and atomically clears and audits only the matching
  lifecycle crash marker. Repeating the operation is idempotent.
- Publication conflicts move the Idea to `Blocked`, post the exact safe
  conflict, and perform no overwrite. Regenerate against a fresh snapshot.
- The v5 publication migration fails closed when a fully applied pre-target
  journal row still has a matching lifecycle run in `publishing`. Inspect the
  journal audit and OpenProject effects, then resolve the lifecycle run through
  the supported recovery procedure before rerunning migration; never backfill
  a guessed target or edit either table by hand.
- Nightly reconciliation selects workflow recovery from the newest run and
  graph authority independently from the newest successfully published run.
  It repairs missed merged-planning-PR events and durable
  implementation-PR/check associations, discovers missed PR head changes, and
  isolates an inaccessible implementation repository instead of aborting the
  remaining scan. It reprojects each work package even when database evidence
  was already committed, restores a missing `Needs Input` status, and unblocks
  or blocks work only when the approved graph makes the transition
  unambiguous. Closed-unmerged implementation evidence becomes a stale finding
  after the configured interval. Latest nonterminal planning runs whose
  existing `updated_at` is at or older than `PLANNING_THREAD_STALE_SECONDS`
  are reported as `stale_threads:<n>` without a repair or timestamp touch.
  Other drift is reported.

Never edit a delivery row, publication journal, LangGraph checkpoint, or
OpenProject managed hash by hand.

Alertmanager retries are safe: the Windmill trigger is synchronous, authenticates
the exact raw request, serializes each fingerprint with a PostgreSQL advisory
lock, and converges a single OpenProject work package. Its bounded internal
retry runs before a terminal failure module returns HTTP 503, so Alertmanager
retains delivery ownership and retries. The latest successful transition time
is read under the same advisory lock; an older firing notification cannot
reopen a resolved alert. The delivery audit contains only the fingerprint,
alert name/state/severity, normalized payload digest, transition time, outcome,
and work-package ID. Each delivery or terminal-failure attempt uses its
Windmill job ID as the audit namespace. Rotate the Alertmanager bearer token by
updating both the AlertmanagerConfig Secret and the worker environment in one
GitOps change.

Crash-only planner start/resume payloads are AES-256-GCM ciphertext bound to
the exact thread and operation purpose. Raw Idea text, repository file
content, and human answers are not stored in lifecycle tables. Back up the
SOPS recovery key with the database; losing it makes only an interrupted
request unrecoverable. Key rotation requires draining lifecycle workers and
re-encrypting any nonterminal ciphertext before the old key is removed.

## Backup and restore

CloudNativePG/Barman is the database backup authority. OpenProject attachments
use the dedicated versioned S3 bucket. A restore drill must create isolated
databases and a test namespace, restore all three databases plus attachment
objects, keep webhook routes disabled, and prove:

1. planner thread reads and an interrupted resume;
2. Windmill delivery/audit history;
3. OpenProject work packages, relations, comments, and attachments;
4. an unchanged approved plan dry-runs to zero operations.

The isolated restore must receive the matching SOPS recovery key without
printing it. Prove that one nonterminal encrypted crash payload can be opened
through the normal service path; do not query or dump its plaintext from SQL.

Delete the isolated restore only after recording backup identifiers, timestamps,
row/object counts, and redacted command results.

## Upgrade and rollback

Change one pinned application image/chart at a time. Back up first, retain the
previous digest, run migrations before workloads, and exercise a canary Idea.
Rollback application images through Git. Never roll a database schema backward
unless the release runbook includes a tested reverse migration; otherwise
restore the pre-upgrade backup into isolated databases and promote only after
validation.

## Image release

Only the exact `v0.1.24` Git tag starts the image workflow. The workflow builds
runtime dependencies from the committed `uv.lock` with hash enforcement,
extends the official Windmill CE image pinned to its exact linux/amd64 digest,
verifies its version, source revision, and CE build identity, emits
SBOM/provenance, and blocks on fixable critical Trivy findings. The Windmill
image also contains the reviewed workspace snapshot at
`/opt/planning-platform-workspace`, so the
one-shot sync Job does not fetch mutable source at runtime. GitOps consumes the
resulting immutable digests, never the mutable display tags.

The Windmill extension installs native Python dependencies with Windmill's
preinstalled uv-managed Python 3.12 and verifies imports with that same
interpreter. The base image's operating-system Python is not the Windmill job
runtime and must not be used to select extension wheels. Image construction
gives root's `uv` operations an ephemeral build-only cache, leaving the base
image's shared cache untouched. The non-root release smoke then locates the
managed interpreter through that normal worker cache path.
