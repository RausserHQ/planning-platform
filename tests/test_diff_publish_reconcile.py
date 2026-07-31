from __future__ import annotations

from pathlib import Path

import pytest

from planning_platform.diff import plan_diff
from planning_platform.loader import load_artifact, load_plan
from planning_platform.openproject import OpenProjectSnapshot, WorkPackageSnapshot, managed_hash
from planning_platform.publisher import PublicationEnvelope, PublicationRejected, publish
from planning_platform.reconciliation import reconcile

FIXTURE = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"


def _plan():
    return load_plan(FIXTURE)


def _artifact():
    return load_artifact(FIXTURE)


def _snapshot(plan, package: WorkPackageSnapshot | None = None, *, etag: str = "fixture"):
    return OpenProjectSnapshot(
        "2026-07-30T00:00:00Z",
        etag,
        plan.plan.openproject_snapshot.sha256,
        () if package is None else (package,),
    )


def _envelope(artifact, snapshot: OpenProjectSnapshot) -> PublicationEnvelope:
    return PublicationEnvelope(
        approved_commit="a" * 40,
        backlog_sha256=artifact.sha256,
        artifact_blob_sha1=artifact.blob_sha1,
        approval_event_id="approval-event-123",
        snapshot_sha256=snapshot.sha256,
        snapshot_etag=snapshot.etag,
        trace_id="trace",
    )


def _hierarchical_plan():
    plan = _plan()
    task = plan.items[0].model_copy(update={"parent": "core-epic", "blocked_by": ("core-epic",)})
    epic = plan.items[0].model_copy(
        update={"key": "core-epic", "type": "Epic", "title": "Core delivery epic"}
    )
    return plan.model_copy(update={"items": (task, epic)})


def test_first_pass_creates_then_stable_parent_and_relations() -> None:
    plan = _hierarchical_plan()
    operations = plan_diff(plan, _snapshot(plan))
    kinds = [operation.kind for operation in operations]
    assert kinds[:2] == ["create_work_package", "create_work_package"]
    parent = next(operation for operation in operations if operation.kind == "set_parent")
    relation = next(operation for operation in operations if operation.kind == "create_relation")
    assert parent.payload == {"parent_identity": (plan.plan.id, "core-epic")}
    assert relation.payload["target_identity"] == (plan.plan.id, "core-epic")
    assert relation.payload["type"] == "blocked_by"
    assert "to_id" not in relation.payload


def test_parent_removal_and_human_relation_preservation() -> None:
    plan = _plan()
    package = WorkPackageSnapshot(
        1,
        3,
        plan.plan.id,
        plan.items[0].key,
        plan.plan.version,
        managed_hash=managed_hash(plan, plan.items[0]),
        parent_identity=(plan.plan.id, "old-parent"),
        relations=(("human-link", 91),),
    )
    operations = plan_diff(plan, _snapshot(plan, package))
    assert any(
        operation.kind == "set_parent" and operation.payload["parent_identity"] is None
        for operation in operations
    )
    assert not any(operation.kind == "remove_managed_relation" for operation in operations)


def test_reapply_is_empty_and_removed_nodes_are_superseded() -> None:
    plan = _plan()
    package = WorkPackageSnapshot(
        1,
        0,
        plan.plan.id,
        plan.items[0].key,
        plan.plan.version,
        managed_hash=managed_hash(plan, plan.items[0]),
        human_fields={"status": "In progress", "assignee": "sam", "comments": ["human"]},
    )
    assert plan_diff(plan, _snapshot(plan, package)) == ()
    retired = WorkPackageSnapshot(9, 3, plan.plan.id, "retired-node", 1, managed_hash="x")
    operations = plan_diff(plan, _snapshot(plan, retired))
    assert any(operation.kind == "mark_superseded" for operation in operations)
    assert package.human_fields["status"] == "In progress"


def test_operation_identity_changes_with_base_snapshot() -> None:
    plan = _plan()
    first = plan_diff(plan, _snapshot(plan))[0]
    changed_base = OpenProjectSnapshot("now", "new-etag", "c" * 64, ())
    second = plan_diff(plan, changed_base)[0]
    assert first.operation_id != second.operation_id


def test_publish_binds_exact_loaded_bytes_and_allows_null_proposal_commit() -> None:
    artifact = _artifact()
    snapshot = _snapshot(artifact.plan)
    applied: list[str] = []

    class Adapter:
        def snapshot(self):
            return snapshot

        def resolve(self, identity):
            return None

        def apply(self, operation, *, idempotency_key, current):
            assert idempotency_key == operation.operation_id
            applied.append(operation.kind)

    result = publish(artifact, Adapter(), _envelope(artifact, snapshot), apply=True)
    assert result.applied
    assert applied == ["create_work_package", "record_audit"]
    create = result.operations[0]
    assert create.payload["planning_commit"] == "a" * 40
    assert create.payload["evidence_state"] == "pending"


def test_replan_does_not_reset_runtime_evidence_state() -> None:
    plan = _plan()
    changed_item = plan.items[0].model_copy(update={"title": "Changed managed title"})
    changed_plan = plan.model_copy(update={"items": (changed_item,)})
    package = WorkPackageSnapshot(
        1,
        7,
        plan.plan.id,
        plan.items[0].key,
        plan.plan.version,
        managed_hash=managed_hash(plan, plan.items[0]),
        human_fields={"evidence_state": "verified", "runtime_failure": "none"},
    )
    operation = next(
        operation
        for operation in plan_diff(changed_plan, _snapshot(plan, package))
        if operation.kind == "update_managed_fields"
    )
    assert "evidence_state" not in operation.payload
    assert "runtime_failure" not in operation.payload


def test_publish_rejects_exact_hash_mismatch_and_stale_snapshot() -> None:
    artifact = _artifact()
    snapshot = _snapshot(artifact.plan)

    class Adapter:
        def snapshot(self):
            return snapshot

        def resolve(self, identity):
            return None

        def apply(self, operation, *, idempotency_key, current):
            raise AssertionError("must not apply")

    invalid_hash = PublicationEnvelope(
        "a" * 40,
        "c" * 64,
        artifact.blob_sha1,
        "approval-event-123",
        snapshot.sha256,
        snapshot.etag,
        "trace",
    )
    with pytest.raises(PublicationRejected, match="SHA-256"):
        publish(artifact, Adapter(), invalid_hash)
    stale = _snapshot(artifact.plan, etag="stale")
    with pytest.raises(PublicationRejected, match="snapshot"):
        publish(artifact, Adapter(), _envelope(artifact, stale))


def test_conflicting_identity_and_stale_reconciliation_offer_no_repair() -> None:
    plan = _plan()
    duplicate = WorkPackageSnapshot(1, 0, plan.plan.id, plan.items[0].key)
    with pytest.raises(ValueError, match="conflicting identity"):
        plan_diff(
            plan,
            OpenProjectSnapshot(
                "now", "fixture", plan.plan.openproject_snapshot.sha256, (duplicate, duplicate)
            ),
        )
    report = reconcile(plan, _snapshot(plan, etag="changed"), approved_commit="a" * 40)
    assert report.findings[0].code == "stale_snapshot"
    assert report.safe_repairs == ()


def test_unapproved_reconciliation_is_read_only() -> None:
    plan = _plan()
    report = reconcile(plan, _snapshot(plan))
    assert report.findings[0].code == "unapproved_plan"
    assert report.safe_repairs == ()
