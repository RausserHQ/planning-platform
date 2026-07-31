# Current-state inventory

Observed 2026-07-30:

| Component | State | Disposition |
|---|---|---|
| GitHub issues and PR templates in `homelab-platform` | Durable work ledger and review surface | Reuse safety/readiness vocabulary; OpenProject becomes ticket authority for this platform |
| OpenSWE `langgraph dev` lab | Experimental coding agent without production checkpoint guarantees | Keep isolated; reuse only GitHub App and network-boundary patterns |
| `RausserHQ/auto-executor` Linear/OMX automation | Separate private Linear-based executor | Freeze for rollback, then replace only after acceptance |
| OpenProject | Absent | Add |
| Windmill | Absent | Add |
| BMad methodology | Absent | Add pinned subset |
| CloudNativePG, Longhorn, MinIO backup, Flux, Cilium, Gateway API, Prometheus | Healthy platform dependencies | Reuse |

The production cluster had seven Ready Talos nodes, Longhorn app/PostgreSQL
storage classes, CloudNativePG and Barman operators, shared Gateway API, and
working Flux reconciliation. One unrelated `minecraft` Flux Kustomization was
reconciling during inventory; this platform does not alter it.

