# Planning Platform

Planning Platform is an open-source, deterministic Idea-to-Tickets system. A
rough OpenProject Idea becomes an approved set of Git planning artifacts and an
idempotently published OpenProject work graph, with GitHub implementation
evidence and scheduled drift reconciliation.

The planner can only emit a typed proposal. Windmill validates and coordinates
every external read or write. Stable ticket identity is always
`plan_id + node_key`.

## Components

```text
apps/planner-api                  private FastAPI + LangGraph planner
packages/backlog-schema          versioned backlog and event contracts
packages/backlog-validator       deterministic semantic validation
packages/planning-artifacts      BMad-derived prompts and renderers
packages/openproject-adapter     snapshot, diff, publish, reconcile
packages/github-adapter          planning PR and evidence integration
windmill                         side-effect scripts and versioned flows
evals                            representative plans and planner scores
docs                             architecture, API, security, and operations
```

Production uses the MIT LangGraph libraries and PostgreSQL checkpointer through
our own API. It does not use the licensed LangGraph Agent Server. Windmill is
built from its AGPL source without the `enterprise` feature; the official Helm
chart is used with that image override.

## Contract lifecycle

`backlog.yaml` is a proposal while `plan.approved_planning_commit` is `null`.
This is necessary because a Git commit cannot contain its own hash. On planning
PR merge, Windmill creates an immutable publication envelope that binds the
merged commit SHA, artifact blob SHA, exact backlog SHA-256, OpenProject
snapshot, and approval event. Apply rejects any mismatch.

See [architecture](docs/architecture.md) and the
[contract rationale](docs/decisions/0001-authority-and-publication-contract.md).

