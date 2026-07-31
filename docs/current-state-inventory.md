# Current-state inventory

Observed 2026-07-30. The disposition vocabulary is deliberately explicit:
**Reused**, **Replaced**, **Frozen for rollback**, or **Removed**.

| Component / exact source | Observed state | Classification | Final boundary or disposition |
|---|---|---|---|
| GitHub Issues and PR templates in `RausserHQ/homelab-platform` | Existing repository work ledger and review surface; `.github/workflows/validate.yml` runs PR/push validation, including immutable planning-artifact validation | Reused | GitHub remains authoritative for source, planning artifacts, PRs, reviews, checks, and implementation evidence. Reuse its safety/readiness vocabulary and planning-PR validation. Do not create or mirror GitHub Issues as planning tickets; OpenProject is the sole planning-ticket tracker. |
| `clusters/prod/apps/openswe/` and `clusters/prod/flux-system/kustomizations/openswe.yaml` in `RausserHQ/homelab-platform` | Separately operated experimental OpenSWE `langgraph dev` lab; its dedicated Funnel ingress accepts only its own GitHub issue-comment route and Flux marks it `production-ready: "false"` | Frozen for rollback | Preserve unchanged and isolated. It is not a planner, ticket publisher, or reconciliation path. Reuse only its documented authentication and network-boundary design lessons—not its credentials, webhook route, state, sandbox, or workload. |
| `.github/workflows/build-openswe-images.yml` and `scripts/validate-openswe-wiring.py` in `RausserHQ/homelab-platform` | Dispatch-only image build and static contract for the isolated OpenSWE lab | Frozen for rollback | Retain as OpenSWE-only maintenance evidence. Never repurpose it for planning-platform builds, GitHub events, or OpenProject automation. |
| `RausserHQ/auto-executor` at observed `main` tree `d3fdd8e23660c480a252ea165551c6d391a808a5` | Private Archon workflow pack, not a standalone service. Entrypoints are `./auto-executor run` / `resume`; workflow definitions are under `.archon/workflows/`. Linear mutations are performed by `.archon/scripts/claim-linear-issue.py`, `update-linear-status.py`, and `release-linear-issue.py`. The repository contains no deployment manifest, webhook configuration, cron/schedule definition, or runtime workload; `.github/workflows/ci.yml` is CI only. | Replaced | Freeze the pack before cutover. Archive only operationally valuable artifacts; do not migrate Linear ticket history. The external Archon installation/invocation and credential owner are not represented in this repository and must be inventoried exactly before cutover. After pilot acceptance, disable that invocation/install, then revoke its Linear mutation credential. Do not invent webhook, schedule, or workload removals that reconnaissance did not find. |
| OpenProject | Absent during reconnaissance | Replaced (target authority) | Replace legacy ticket tracking with the sole authority for Idea/work-package hierarchy, typed relations, status, assignee, and effort. |
| Windmill | Absent during reconnaissance | Replaced (target runtime) | Replace legacy side-effect automation with the sole planning event receiver/processor for retries, deduplication, publication, and reconciliation. |
| BMad planning methodology | Not previously installed | Reused | Consume a pinned, attributed subset as artifact, prompt, and checklist methodology; it is not a runtime authority. |
| CloudNativePG, Longhorn, MinIO backup, Flux, Cilium, Gateway API, and Prometheus | Existing healthy platform dependencies | Reused | Consume through the established GitOps, SOPS, storage, backup, ingress, and observability boundaries. |

The production cluster had seven Ready Talos nodes, Longhorn app/PostgreSQL
storage classes, CloudNativePG and Barman operators, shared Gateway API, and
working Flux reconciliation. One unrelated `minecraft` Flux Kustomization was
reconciling during inventory; this platform does not alter it.

This inventory is a source-controlled design-time record, not an assertion of
live mutation. OpenSWE remains a separate experimental coding-agent lab: its
issue-comment Funnel webhook and GitHub App are not planning-platform event
routes. The planning platform receives its own signed GitHub and OpenProject
ingress through Windmill and shares no OpenSWE credentials, state, schedules,
or webhook endpoints.
