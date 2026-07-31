# ADR 0003: Official Windmill CE base for personal deployment

- Status: Accepted
- Date: 2026-07-31
- Supersedes: ADR 0002

## Context

Building Windmill 1.775.2 from source added roughly 50 minutes to every release.
The owner directed this personal, non-commercial deployment to use the existing
official Community Edition image instead.

## Decision

Use the official `ghcr.io/windmill-labs/windmill:1.775.2` linux/amd64 image at
the exact reviewed platform-manifest digest. Before building the extension, the
release must verify the image OS and architecture, OCI version, source revision,
upstream source URL, CE build history, and absence of an EE build marker.

Build only the Planning Platform extension layer. Continue to emit derived-image
SBOM and provenance, scan the final digest for fixable critical vulnerabilities,
and verify the runtime package, workspace, non-root user, npm security update,
and Windmill CLI contract before promotion.

## Consequences

Release time is substantially shorter and the base remains immutable and
auditable. The runtime is no longer represented as a pure source build. The
upstream mixed-license and Community Edition binary terms apply, so this choice
is documented as personal and non-commercial and must be reviewed before the
derived image is redistributed or the use changes.
