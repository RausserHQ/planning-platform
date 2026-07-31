# Security model

The planner accepts typed immutable context and has no OpenProject, GitHub, or
Windmill credential. Windmill alone receives mutation credentials and is the
only component allowed to coordinate external effects.

- GitHub App tokens are minted for one installation, cached only in memory,
  and renewed before expiry. The App needs repository contents and pull-request
  read/write plus checks/reviews read; it needs no administration or issue
  permission.
- OpenProject uses a project-scoped non-admin service user. Its API token and
  the separate webhook HMAC secret are SOPS-managed and mounted only in
  bootstrap/worker pods.
- The internal planner token authenticates every `/v1` request. Health and
  metrics are the only unauthenticated planner endpoints.
- HTTP ingress preserves the exact raw body. Lifecycle metadata is parsed only
  from those HMAC-verified bytes; a mismatched parsed body, missing timestamp,
  stale delivery, invalid signature, or oversized body is rejected.
- Delivery ownership uses PostgreSQL time, rotating UUID claim tokens, and
  heartbeats. A stale owner cannot finalize a successor's claim.
- Lifecycle crash recovery stores only AES-256-GCM ciphertext, authenticated
  against the exact thread and request purpose. Its 32-byte key is
  SOPS-managed and unavailable to the planner. Development-era plaintext
  recovery columns are explicitly dropped by the pre-release migration.
- Publication binds merge commit, Git blob SHA-1, artifact SHA-256, snapshot,
  plan identity, and approval delivery. Writes use OpenProject forms,
  optimistic locks, deterministic markers, and a durable intent/outcome
  journal.
- Logs and audit records contain identifiers and sanitized classifications,
  never tokens, private keys, raw webhook bodies, arbitrary exception text, or
  repository file content.

Production NetworkPolicies deny ingress and egress by default. The planner may
reach only its database and model API; it cannot reach GitHub or OpenProject.
OpenProject and Windmill UIs are LAN-only. The sole public endpoint is the
GitHub webhook path, carried through the approved tunnel and verified before
any durable effect.

Release jobs are tag-only. Every privileged third-party action is pinned to a
commit, Python runtime dependencies are installed from the hash-bearing lock
export, images include SBOM/provenance attestations, and a pinned Trivy action
rejects fixable critical findings before a digest is promoted to GitOps.
