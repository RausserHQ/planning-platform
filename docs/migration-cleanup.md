# Migration and cleanup

## Scope and guardrails

The legacy OpenSWE/LangGraph lab and private Linear/OMX executor are outside
this platform. Their classifications are recorded in
[`current-state-inventory.md`](current-state-inventory.md).

OpenSWE is frozen as an isolated experimental rollback point, not a migration
source. Retain without change its `langgraph dev` lab, issue-comment GitHub
Funnel route, GitHub App credentials, sandbox state, image-build workflow, and
OpenSWE-specific validation. The planning platform must not route events
through, share credentials with, or use OpenSWE as an execution or ticketing
component.

GitHub Issues are not migrated or mirrored into OpenProject merely for
completeness. GitHub remains the repository, planning-artifact, pull-request,
review, check, and implementation-evidence authority. OpenProject is the sole
authority for planning tickets, hierarchy, dependencies, status, assignee, and
effort.

Do not delete legacy data, credentials, workloads, manifests, webhook routes,
or schedules before all three acceptance pilots in
[`acceptance.md`](acceptance.md) pass with recorded live evidence. After the
third pilot passes, retain the final rollback point for 30 calendar days.

## Cutover record

Before a legacy path is disabled, create a redacted, reviewable migration
record containing only:

- the approved planning-platform commit and deployment revision;
- the three pilot Idea, plan, and trace identifiers plus the acceptance
  evidence location;
- each legacy component, owning repository, exact invocation, installation,
  webhook, schedule, workload, and credential-owner identifiers that actually
  exist, plus disposition, operator, and rollback point;
- the artifact export/archive location, 30-day retention expiry, and restore
  instructions; and
- verification proving event exclusivity and absence of duplicate automation.

Never include tokens, webhook secrets, kubeconfigs, private keys, database
URLs, or raw payloads.

## Ordered cleanup after acceptance

1. Export only planning artifacts with continuing operational or planning
   value. Do not migrate historical ticket noise.
2. Freeze the final pre-cutover commit/release and create the documented backup
   or export needed for the 30-day rollback window.
3. Resolve the currently external owner/location of the Archon installation or
   invocation that runs `RausserHQ/auto-executor`'s `./auto-executor run` and
   `resume` entrypoints. Record its exact identifier and trigger before
   proceeding. Reconnaissance found no webhook, schedule, deployment, or
   runtime workload in that repository; do not claim or disable one without new
   evidence. Disable the actual external invocation/install through its owning
   reviewed workflow and record a redacted post-disable execution check. Do not
   disable OpenSWE's isolated issue-comment webhook as part of this cutover.
4. Verify that Windmill alone receives and processes planning OpenProject and
   GitHub events, while OpenSWE receives only its experimental issue-comment
   events. Verify no Linear/OMX path can mutate the new OpenProject project or
   GitHub planning artifacts.
5. Revoke the obsolete Linear mutation credential used by
   `.archon/scripts/claim-linear-issue.py`, `update-linear-status.py`, and
   `release-linear-issue.py` only after the actual Archon invocation is disabled
   and event exclusivity passes. Preserve OpenSWE credentials while its
   isolated lab remains supported.
6. Remove an obsolete external Archon installation/workload only if the cutover
   inventory proves it exists, and then only through its owning reviewed
   desired-state workflow. The source workflow pack may remain frozen for the
   30-day rollback window. Do not remove OpenSWE paths under
   `clusters/prod/apps/openswe/`, its Flux Kustomization, Funnel ingress,
   image-build workflow, or validators. Never substitute an ad hoc live
   deletion for a desired-state change.
7. Retain the rollback point until the recorded expiry. A rollback must first
   disable Windmill planning mutations, prove they are inactive, and only then
   reactivate the frozen executor. Never enable both publishers concurrently.
8. At expiry, separately approve any irreversible deletion, then update
   architecture and operations documentation to describe only supported paths.

## Verification evidence

Before and after cleanup, record redacted output from the owning repositories'
normal checks. At minimum, `RausserHQ/homelab-platform` must pass:

```bash
python3 scripts/validate-openswe-wiring.py
python3 -m unittest \
  tests.test_openswe_wiring \
  tests.test_openswe_github_funnel \
  tests.test_validate_github_workflows
scripts/validate-github-workflows.py
```

Live evidence must name the bounded GitOps revision, show the planning
Kustomizations and Windmill workloads Ready, prove one accepted and one
rejected signed delivery on each planning ingress, and show zero delivery or
mutation by the disabled Linear/OMX path. Capture identifiers and outcomes, not
secret-bearing payloads. Use only the access wrappers and execution lanes
documented by the owning repository.

## Completion criteria

Cleanup is complete only when the redacted record proves:

- all three pilots passed;
- OpenProject is the only planning-ticket authority;
- GitHub Issues are not a competing tracker;
- Windmill is the only planning external-side-effect runtime;
- no duplicate legacy webhook, schedule, or publisher remains active;
- OpenSWE remains clearly documented as an isolated experimental lab; and
- rollback ownership and the 30-day expiry are recorded.
