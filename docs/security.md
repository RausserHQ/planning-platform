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
- Alertmanager reaches one internal Windmill trigger with a separate
  SOPS-managed bearer token. The trigger accepts only bounded alerts labeled
  `area=planning-platform`; Windmill converges one OpenProject Task per
  fingerprint, assigns its creation to the one least-privilege human alert
  recipient, and never mirrors alerts into GitHub Issues. The synchronous
  trigger returns a retryable failure to Alertmanager until OpenProject
  delivery and its sanitized audit commit both succeed.
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
any durable effect. Alert delivery stays cluster-internal from Alertmanager to
Windmill and then uses the existing project-scoped OpenProject publisher token.

Release jobs are tag-only. Every privileged third-party action is pinned to a
commit, Python runtime dependencies are installed from the hash-bearing lock
export, images include SBOM/provenance attestations, and a pinned Trivy action
rejects fixable critical findings before a digest is promoted to GitOps. The
Windmill extension starts from the official CE linux/amd64 image pinned by
digest. The release verifies the base image's version, source-revision labels,
and CE build history before use. It then upgrades the runtime npm bundle to
exact npm `11.19.0` and asserts its fixed `tar` `7.5.19` dependency before
installing the pinned Windmill CLI; this closes the upstream runtime image's
fixable `CVE-2026-59873` without weakening the scanner.
