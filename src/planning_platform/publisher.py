"""Fail-closed, adapter-injected publication orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .diff import PublicationOperation, plan_diff
from .loader import LoadedArtifact
from .models import with_approved_commit
from .openproject import OpenProjectSnapshot, WorkPackageSnapshot
from .validation import SemanticValidationError, validate_plan

if TYPE_CHECKING:
    from .publication_journal import PublicationJournal

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

    def postcondition(self, operation: PublicationOperation) -> bool: ...


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
    publication_identity: str = ""


@dataclass(frozen=True)
class PublishResult:
    operations: tuple[PublicationOperation, ...]
    applied: bool


def _check_artifact_envelope(artifact: LoadedArtifact, envelope: PublicationEnvelope) -> None:
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
    if envelope.publication_identity != artifact.plan.plan.publication_identity:
        raise PublicationRejected("immutable envelope has an invalid plan publication identity")


def _check_envelope(
    artifact: LoadedArtifact, envelope: PublicationEnvelope, snapshot: OpenProjectSnapshot
) -> None:
    _check_artifact_envelope(artifact, envelope)
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
    if current is None or "expected_parent_identity" not in conditions:
        return
    expected_parent = conditions["expected_parent_identity"]
    if isinstance(expected_parent, list):
        expected_parent = tuple(expected_parent)
    expected_unmanaged = conditions.get("expected_unmanaged_parent")
    expected_relations = conditions.get("expected_managed_relations")
    if not isinstance(expected_unmanaged, bool) or not isinstance(expected_relations, list):
        raise PublicationRejected(f"topology precondition is malformed for {operation.identity}")
    actual_unmanaged = current.parent_id is not None and current.parent_identity is None
    if current.parent_identity != expected_parent or actual_unmanaged != expected_unmanaged:
        raise PublicationRejected(f"parent topology changed for {operation.identity}")
    normalized = tuple(
        sorted(
            (str(relation[0]), (str(relation[1][0]), str(relation[1][1])))
            for relation in expected_relations
            if isinstance(relation, (list, tuple))
            and len(relation) == 2
            and isinstance(relation[1], (list, tuple))
            and len(relation[1]) == 2
        )
    )
    if (
        len(normalized) != len(expected_relations)
        or tuple(sorted(current.managed_relations)) != normalized
    ):
        raise PublicationRejected(f"managed relation topology changed for {operation.identity}")


def _publish(
    artifact: LoadedArtifact,
    adapter: PublicationAdapter,
    envelope: PublicationEnvelope,
    *,
    apply: bool = False,
    journal: PublicationJournal | None = None,
) -> PublishResult:
    """Validate, bind exact bytes, stale-check, then apply refreshed mutations."""
    issues = validate_plan(artifact.plan)
    if issues:
        raise SemanticValidationError(issues)
    _check_artifact_envelope(artifact, envelope)
    if apply:
        if journal is None or not journal.ready():
            raise PublicationRejected("apply requires a ready durable publication journal")
        try:
            resumed = journal.resume(envelope)
        except ValueError as error:
            raise PublicationRejected(str(error)) from error
        if resumed is not None:
            recorded_operations, completed = resumed
            for operation in recorded_operations:
                if operation.operation_id in completed:
                    continue
                state = journal.state(operation)
                # Only a request that was actually attempted can be recovered
                # from an observed postcondition. A planned/retryable operation
                # must pass current-state checks immediately before its intent.
                if state in {"intent", "ambiguous"}:
                    if adapter.postcondition(operation):
                        journal.outcome(operation, result="recovered")
                        continue
                    if state == "intent":
                        from .publication_journal import AmbiguousPublicationEffect

                        journal.failure(
                            operation,
                            AmbiguousPublicationEffect(
                                "interrupted publication intent has no confirmed postcondition"
                            ),
                        )
                    raise PublicationRejected(
                        f"publication operation remains ambiguous: {operation.operation_id}"
                    )
                current = (
                    None
                    if operation.kind == "record_audit"
                    else adapter.resolve(operation.identity)
                )
                _assert_current(operation, current)
                journal.intent(operation)
                try:
                    adapter.apply(
                        operation,
                        idempotency_key=operation.operation_id,
                        current=current,
                    )
                except Exception as error:
                    journal.failure(operation, error)
                    raise
                journal.outcome(operation, result="recovered")
            journal.finalize()
            return PublishResult(recorded_operations, applied=True)
    snapshot = adapter.snapshot()
    _check_envelope(artifact, envelope, snapshot)
    publication_plan = with_approved_commit(artifact.plan, envelope.approved_commit)
    operations = plan_diff(publication_plan, snapshot, trace_id=envelope.trace_id)
    if apply:
        assert journal is not None
        recorded_operations, completed = journal.begin(envelope, operations)
        for operation in recorded_operations:
            if operation.operation_id in completed:
                continue
            current = (
                None if operation.kind == "record_audit" else adapter.resolve(operation.identity)
            )
            _assert_current(operation, current)
            journal.intent(operation)
            try:
                adapter.apply(operation, idempotency_key=operation.operation_id, current=current)
            except Exception as error:
                journal.failure(operation, error)
                raise
            journal.outcome(operation)
        journal.finalize()
        operations = recorded_operations
    return PublishResult(operations=operations, applied=apply)


def publish(
    artifact: LoadedArtifact,
    adapter: PublicationAdapter,
    envelope: PublicationEnvelope,
    *,
    apply: bool = False,
    journal: PublicationJournal | None = None,
) -> PublishResult:
    """Publish with unconditional release of any durable journal session fence."""
    try:
        return _publish(artifact, adapter, envelope, apply=apply, journal=journal)
    finally:
        if journal is not None:
            close = getattr(journal, "close", None)
            if callable(close):
                close()
