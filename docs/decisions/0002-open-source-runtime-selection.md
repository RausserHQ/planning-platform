# ADR 0002: Fully open-source runtime selection

- Status: Accepted
- Date: 2026-07-30

## Decision

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

The platform has no runtime license-key dependency on LangChain. Windmill image
builds are slower and must carry AGPL source/offering obligations. Upstream
versions are explicit and reviewable.

