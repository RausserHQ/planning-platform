# ADR 0002: Fully open-source runtime selection

- Status: Superseded by ADR 0003 on 2026-07-31
- Date: 2026-07-30

## Decision

This section records the original decision and is no longer active.

Build the planner as a FastAPI service around the MIT LangGraph libraries and
`langgraph-checkpoint-postgres`. Do not use the licensed LangGraph Agent
Server.

Build Windmill v1.775.2 from source without the `enterprise` feature and deploy
it with official Helm chart 4.0.223. Do not use the vendor's ready-made
Community image because its license covers proprietary components.

Adapt only the planning methodology, prompts, checklists, and artifact
structure needed from BMad Method v6.10.0 commit
`081e64ee5aab2316b912883f7bee528ee143ce36`; retain its MIT attribution.

Deploy OpenProject Community 17.6.0 with official Helm chart 13.9.0.

## Consequences

The platform had no runtime license-key dependency on LangChain. Windmill image
builds were slower and carried AGPL source/offering obligations. Upstream
versions remained explicit and reviewable.
