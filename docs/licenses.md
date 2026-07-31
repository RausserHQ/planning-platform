# License and source obligations

Planning Platform is licensed under Apache-2.0. Its dependency lock and image
SBOM are release artifacts.

| Component | Pin | License / obligation |
|---|---|---|
| BMad Method | 6.10.0, `081e64ee5aab2316b912883f7bee528ee143ce36` | MIT; attribution and license retained under `packages/planning-artifacts/` |
| LangGraph libraries | versions in `uv.lock` | MIT; used as libraries in the private planner |
| OpenProject Community | 17.6.0, chart 13.9.0 | GPL-3.0; unmodified official image/source link exposed in operations documentation |
| Windmill Community | 1.775.2, official CE linux/amd64 digest `sha256:ef39329523f4806e5cd5169ffa7af2618f39439bcf659115e8bb804c592d7132`, revision `64ec1aa490ab5537a73bcb6f6b7e926c6339af39`, chart 4.0.223 | Upstream mixed AGPL-3.0, Apache-2.0, and Community Edition binary terms apply; this personal deployment pins and verifies the vendor image identity |
| Planning Platform Python dependencies | exact versions in `uv.lock` | Notices and license inventory emitted by the release SBOM |

The Windmill runtime extends the official Community Edition image and adds the
Planning Platform wheel, reviewed workspace, npm security update, and matching
CLI. The upstream image is not represented as a pure open-source build. Keep
its exact vendor digest and revision evidence with this repository commit,
`deploy/windmill/extend.Dockerfile`, and the derived runtime SBOM/provenance.
This selection is scoped to the owner's personal, non-commercial deployment;
review the current upstream Community Edition terms before redistributing the
derived image or changing that use.
