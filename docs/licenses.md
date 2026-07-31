# License and source obligations

Planning Platform is licensed under Apache-2.0. Its dependency lock and image
SBOM are release artifacts.

| Component | Pin | License / obligation |
|---|---|---|
| BMad Method | 6.10.0, `081e64ee5aab2316b912883f7bee528ee143ce36` | MIT; attribution and license retained under `packages/planning-artifacts/` |
| LangGraph libraries | versions in `uv.lock` | MIT; used as libraries in the private planner |
| OpenProject Community | 17.6.0, chart 13.9.0 | GPL-3.0; unmodified official image/source link exposed in operations documentation |
| Windmill Community | 1.775.2, `64ec1aa490ab5537a73bcb6f6b7e926c6339af39`, chart 4.0.223 | AGPL-3.0; built from exact public source without `enterprise`; source commit and corresponding source image accompany the runtime image |
| Planning Platform Python dependencies | exact versions in `uv.lock` | Notices and license inventory emitted by the release SBOM |

The Windmill runtime image is derived from the named upstream commit and adds
only the Planning Platform wheel plus a matching CLI. Anyone receiving network
service from that image must be offered the exact corresponding source:

- upstream Windmill commit and unmodified build definition;
- this repository commit and `deploy/windmill/extend.Dockerfile`;
- build provenance and SBOM for both source and runtime images.

Do not replace the source-built image with Windmill's mixed-license vendor
image without a new license review.
