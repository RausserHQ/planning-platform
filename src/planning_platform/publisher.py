"""Fail-closed, adapter-injected publication orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .diff import PublicationOperation, plan_diff
from .loader import LoadedArtifact
from .models import with_approved_commit
from .openproject import OpenProjectSnapshot, WorkPackageSnapshot
from .validation import SemanticValidationError, validate_plan

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PublicationAdapter(Protocol):
    """The sole mutation seam; it refreshes identity/lock state before each effect."""

    def snapshot(self) -> OpenProjectSnapshot: ...

    def resolve(self, identity: tuple[str, str]) -> WorkPackageSnapshot | None: ...

    def apply(
        self,
        operation: PublicationOperation,
        *,
        idempotency_key: str,
        current: WorkPackageSnapshot | None,
    ) -> None: ...


class PublicationRejected(ValueError):
    pass


@dataclass(frozen=True)
class PublicationEnvelope:
    approved_commit: str
    backlog_sha256: str
    artifact_blob_sha1: str
    approval_event_id: str
    snapshot_sha256: str
    snapshot_etag: str
    trace_id: str


@dataclass(frozen=True)
class PublishResult:
    operations: tuple[PublicationOperation, ...]
    applied: bool


def _check_envelope(
    artifact: LoadedArtifact, envelope: PublicationEnvelope, snapshot: OpenProjectSnapshot
) -> None:
    if not _SHA1.fullmatch(envelope.approved_commit):
        raise PublicationRejected("immutable envelope has an invalid approved commit")
    if not _SHA1.fullmatch(envelope.artifact_blob_sha1):
        raise PublicationRejected("immutable envelope has an invalid artifact blob SHA")
    if not _SHA256.fullmatch(envelope.backlog_sha256) or not envelope.approval_event_id:
        raise PublicationRejected("immutable envelope has invalid approval identity")
    if artifact.sha256 != envelope.backlog_sha256:
        raise PublicationRejected("artifact SHA-256 does not match immutable envelope")
    if artifact.blob_sha1 != envelope.artifact_blob_sha1:
        raise PublicationRejected("artifact blob SHA does not match immutable envelope")
    approved = artifact.plan.plan.approved_planning_commit
    if approved is not None and approved != envelope.approved_commit:
        raise PublicationRejected("approved commit does not match immutable envelope")
    reference = artifact.plan.plan.openproject_snapshot
    if reference.sha256 != envelope.snapshot_sha256 or reference.etag != envelope.snapshot_etag:
        raise PublicationRejected("plan snapshot does not match immutable envelope")
    if snapshot.sha256 != envelope.snapshot_sha256 or snapshot.etag != envelope.snapshot_etag:
        raise PublicationRejected("stale OpenProject snapshot")


def _assert_current(operation: PublicationOperation, current: WorkPackageSnapshot | None) -> None:
    conditions = operation.preconditions
    if conditions.get("identity_absent") and current is not None:
        raise PublicationRejected(f"identity already exists: {operation.identity}")
    if conditions.get("identity_resolved") and current is None:
        raise PublicationRejected(f"identity cannot be resolved: {operation.identity}")
    expected_hash = conditions.get("expected_managed_hash")
    if current is not None and expected_hash is not None and current.managed_hash != expected_hash:
        raise PublicationRejected(f"managed state changed for {operation.identity}")


def publish(
    artifact: LoadedArtifact,
    adapter: PublicationAdapter,
    envelope: PublicationEnvelope,
    *,
    apply: bool = False,
) -> PublishResult:
    """Validate, bind exact bytes, stale-check, then apply refreshed mutations."""
    issues = validate_plan(artifact.plan)
    if issues:
        raise SemanticValidationError(issues)
    snapshot = adapter.snapshot()
    _check_envelope(artifact, envelope, snapshot)
    publication_plan = with_approved_commit(artifact.plan, envelope.approved_commit)
    operations = plan_diff(publication_plan, snapshot, trace_id=envelope.trace_id)
    if apply:
        for operation in operations:
            current = (
                None if operation.kind == "record_audit" else adapter.resolve(operation.identity)
            )
            _assert_current(operation, current)
            adapter.apply(operation, idempotency_key=operation.operation_id, current=current)
    return PublishResult(operations=operations, applied=apply)
