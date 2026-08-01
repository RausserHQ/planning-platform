from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from planning_platform.diff import plan_diff
from planning_platform.loader import load_artifact, load_artifact_bytes, load_plan
from planning_platform.models import ReplanScope, with_approved_commit
from planning_platform.openproject import OpenProjectSnapshot, WorkPackageSnapshot, managed_hash
from planning_platform.publication_journal import InMemoryPublicationJournal
from planning_platform.publisher import (
    PublicationEnvelope,
    PublicationRejected,
    ReplanPublicationContext,
    publish,
)
from planning_platform.reconciliation import reconcile
from planning_platform.replan import apply_replan_boundary

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
        publication_target_sha256="e" * 64,
        publication_identity=artifact.plan.plan.publication_identity,
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


def test_one_sided_reverse_related_to_has_canonical_global_owner() -> None:
    plan = _plan()
    lower = plan.items[0].model_copy(update={"key": "a", "related_to": ()})
    higher = lower.model_copy(update={"key": "z", "related_to": ("a",)})
    related = plan.model_copy(update={"items": (higher, lower)})
    operations = plan_diff(related, _snapshot(related))
    relation = next(operation for operation in operations if operation.kind == "create_relation")
    assert relation.identity == (related.plan.id, "a")
    assert relation.payload == {"type": "related_to", "target_identity": (related.plan.id, "z")}


def test_related_to_snapshot_replay_does_not_oscillate_between_endpoints() -> None:
    plan = _plan()
    lower = plan.items[0].model_copy(update={"key": "a", "related_to": ()})
    higher = lower.model_copy(update={"key": "z", "related_to": ("a",)})
    related = plan.model_copy(update={"items": (higher, lower)})
    snapshot = OpenProjectSnapshot(
        "now",
        "fixture",
        related.plan.openproject_snapshot.sha256,
        (
            WorkPackageSnapshot(
                1,
                1,
                related.plan.id,
                "a",
                related.plan.version,
                managed_hash=managed_hash(related, lower),
                managed_relations=(("related_to", (related.plan.id, "z")),),
            ),
            WorkPackageSnapshot(
                2,
                1,
                related.plan.id,
                "z",
                related.plan.version,
                managed_hash=managed_hash(related, higher),
                managed_relations=(),
            ),
        ),
    )
    assert plan_diff(related, snapshot) == ()


def test_topology_preconditions_distinguish_unmanaged_parent_from_none() -> None:
    plan = _plan()
    package = WorkPackageSnapshot(
        1,
        1,
        plan.plan.id,
        plan.items[0].key,
        plan.plan.version,
        managed_hash=managed_hash(plan, plan.items[0]),
        parent_id=900,
        parent_identity=None,
    )
    operation = next(
        operation
        for operation in plan_diff(plan, _snapshot(plan, package))
        if operation.kind == "set_parent"
    )
    assert operation.preconditions["expected_parent_identity"] is None
    assert operation.preconditions["expected_unmanaged_parent"] is True
    assert operation.preconditions["expected_managed_relations"] == []


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


def test_reintroduced_superseded_node_is_reactivated_once() -> None:
    plan = _plan()
    package = WorkPackageSnapshot(
        1,
        4,
        plan.plan.id,
        plan.items[0].key,
        plan.plan.version,
        managed_hash=managed_hash(plan, plan.items[0]),
        human_fields={"status_id": 29},
        superseded=True,
    )
    operations = plan_diff(plan, _snapshot(plan, package))
    assert [operation.kind for operation in operations] == [
        "reactivate_work_package",
        "record_audit",
    ]
    assert operations[0].payload == {"status": "Ready"}

    ready = WorkPackageSnapshot(
        **{
            **package.__dict__,
            "human_fields": {"status_id": 24},
            "superseded": False,
        }
    )
    assert plan_diff(plan, _snapshot(plan, ready)) == ()


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
        publication_target_sha256 = "e" * 64

        def snapshot(self):
            return snapshot

        def resolve(self, identity):
            return None

        def apply(self, operation, *, idempotency_key, current):
            assert idempotency_key == operation.operation_id
            applied.append(operation.kind)

    result = publish(
        artifact,
        Adapter(),
        _envelope(artifact, snapshot),
        apply=True,
        journal=InMemoryPublicationJournal(),
    )
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


def test_partial_replan_emits_no_managed_update_for_protected_nodes() -> None:
    prior = _plan()
    root = prior.items[0]
    protected = root.model_copy(update={"key": "protected-node", "title": "Human-safe node"})
    prior = prior.model_copy(update={"items": (root, protected)})
    approved_prior = with_approved_commit(prior, "c" * 40)
    proposed = prior.model_copy(
        update={
            "plan": prior.plan.model_copy(
                update={
                    "version": 2,
                    "publication_identity": f"{prior.plan.id}:v2",
                }
            ),
            "items": (root.model_copy(update={"title": "Changed root"}), protected),
        }
    )
    replanned = apply_replan_boundary(
        prior,
        proposed,
        base_approved_commit="c" * 40,
        selected_root_keys=(root.key,),
        affected_node_keys=(root.key,),
    )
    approved_replan = with_approved_commit(replanned, "d" * 40)
    snapshot = OpenProjectSnapshot(
        captured_at=prior.plan.openproject_snapshot.captured_at,
        etag=prior.plan.openproject_snapshot.etag,
        sha256=prior.plan.openproject_snapshot.sha256,
        work_packages=(
            WorkPackageSnapshot(
                id=101,
                lock_version=1,
                plan_id=prior.plan.id,
                node_key=root.key,
                plan_version=1,
                managed_hash=managed_hash(approved_prior, root),
            ),
            WorkPackageSnapshot(
                id=102,
                lock_version=1,
                plan_id=prior.plan.id,
                node_key=protected.key,
                plan_version=1,
                managed_hash=managed_hash(approved_prior, protected),
                human_fields={"assignee": "sam", "notes": "retain"},
            ),
        ),
    )

    operations = plan_diff(approved_replan, snapshot)

    updated = [
        operation.identity[1]
        for operation in operations
        if operation.kind == "update_managed_fields"
    ]
    assert updated == [root.key]
    assert all(operation.identity[1] != protected.key for operation in operations)


def test_chained_replans_carry_each_protected_nodes_original_binding() -> None:
    base = _plan()
    first = base.items[0]
    second = first.model_copy(update={"key": "second-node", "title": "Second node"})
    third = first.model_copy(update={"key": "third-node", "title": "Third node"})
    base = base.model_copy(update={"items": (first, second, third)})
    version_two_proposal = base.model_copy(
        update={
            "plan": base.plan.model_copy(
                update={"version": 2, "publication_identity": f"{base.plan.id}:v2"}
            ),
            "items": (first.model_copy(update={"title": "First v2"}), second, third),
        }
    )
    version_two = apply_replan_boundary(
        base,
        version_two_proposal,
        base_approved_commit="c" * 40,
        selected_root_keys=(first.key,),
        affected_node_keys=(first.key,),
    )
    version_three_proposal = version_two.model_copy(
        update={
            "plan": version_two.plan.model_copy(
                update={"version": 3, "publication_identity": f"{base.plan.id}:v3"}
            ),
            "items": (
                first,
                second.model_copy(update={"title": "Second v3"}),
                third,
            ),
        }
    )
    version_three = apply_replan_boundary(
        version_two,
        version_three_proposal,
        base_approved_commit="d" * 40,
        selected_root_keys=(second.key,),
        affected_node_keys=(second.key,),
    )

    assert version_three.plan.replan is not None
    bindings = {
        binding.node_key: (binding.plan_version, binding.planning_commit)
        for binding in version_three.plan.replan.retained_node_bindings
    }
    assert bindings == {
        first.key: (2, "d" * 40),
        third.key: (1, "c" * 40),
    }


def test_publish_requires_immutable_base_and_rechecks_partial_replan_boundary() -> None:
    original = _plan()
    selected = original.items[0]
    unrelated = selected.model_copy(
        update={"key": "unrelated-root", "title": "Unrelated root"}
    )
    base = original.model_copy(update={"items": (selected, unrelated)})
    base_artifact = load_artifact_bytes(
        yaml.safe_dump(base.model_dump(mode="json"), sort_keys=False).encode()
    )
    proposal = base.model_copy(
        update={
            "plan": base.plan.model_copy(
                update={"version": 2, "publication_identity": f"{base.plan.id}:v2"}
            ),
            "items": (
                selected.model_copy(update={"title": "Authorized change"}),
                unrelated.model_copy(update={"title": "Escaped change"}),
            ),
        }
    )
    escaped = proposal.model_copy(
        update={
            "plan": proposal.plan.model_copy(
                update={
                    "replan": ReplanScope(
                        base_plan_version=1,
                        selected_root_keys=(selected.key, unrelated.key),
                        affected_node_keys=(selected.key, unrelated.key),
                        retained_node_bindings=(),
                    )
                }
            )
        }
    )
    escaped_artifact = load_artifact_bytes(
        yaml.safe_dump(escaped.model_dump(mode="json"), sort_keys=False).encode()
    )
    snapshot = _snapshot(escaped)

    class Adapter:
        publication_target_sha256 = "e" * 64

        def snapshot(self):
            return snapshot

    with pytest.raises(PublicationRejected, match="immutable published base"):
        publish(
            escaped_artifact,
            Adapter(),  # type: ignore[arg-type]
            _envelope(escaped_artifact, snapshot),
        )

    context = ReplanPublicationContext(
        base_artifact=base_artifact,
        base_approved_commit="c" * 40,
        selected_root_keys=(selected.key,),
        affected_node_keys=(selected.key,),
    )
    with pytest.raises(PublicationRejected, match="operator-authorized"):
        publish(
            escaped_artifact,
            Adapter(),  # type: ignore[arg-type]
            _envelope(escaped_artifact, snapshot),
            replan_context=context,
        )

    bounded = apply_replan_boundary(
        base,
        proposal.model_copy(
            update={
                "items": (
                    selected.model_copy(update={"title": "Authorized change"}),
                    unrelated,
                )
            }
        ),
        base_approved_commit="c" * 40,
        selected_root_keys=(selected.key,),
        affected_node_keys=(selected.key,),
    )
    bounded_artifact = load_artifact_bytes(
        yaml.safe_dump(bounded.model_dump(mode="json"), sort_keys=False).encode()
    )
    bounded_snapshot = _snapshot(bounded)

    result = publish(
        bounded_artifact,
        Adapter(),  # type: ignore[arg-type]
        _envelope(bounded_artifact, bounded_snapshot),
        replan_context=context,
    )

    assert result.applied is False


def test_publish_rejects_exact_hash_mismatch_and_stale_snapshot() -> None:
    artifact = _artifact()
    snapshot = _snapshot(artifact.plan)

    class Adapter:
        publication_target_sha256 = "e" * 64

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
        "e" * 64,
        artifact.plan.plan.publication_identity,
    )
    with pytest.raises(PublicationRejected, match="SHA-256"):
        publish(artifact, Adapter(), invalid_hash)
    stale = _snapshot(artifact.plan, etag="stale")
    with pytest.raises(PublicationRejected, match="snapshot"):
        publish(artifact, Adapter(), _envelope(artifact, stale))


def test_publication_resume_cannot_cross_openproject_targets() -> None:
    artifact = _artifact()
    snapshot = _snapshot(artifact.plan)
    envelope = _envelope(artifact, snapshot)
    operations = plan_diff(
        artifact.plan.model_copy(
            update={
                "plan": artifact.plan.plan.model_copy(
                    update={"approved_planning_commit": envelope.approved_commit}
                )
            }
        ),
        snapshot,
        trace_id=envelope.trace_id,
    )
    journal = InMemoryPublicationJournal()
    journal.begin(envelope, operations)
    journal.intent(operations[0])
    journal.failure(operations[0], RuntimeError("retryable"))
    journal.close()

    class WrongTargetAdapter:
        publication_target_sha256 = "f" * 64

        def snapshot(self):
            raise AssertionError("target mismatch must precede snapshot or replay")

    with pytest.raises(PublicationRejected, match="target changed"):
        publish(
            artifact,
            WrongTargetAdapter(),  # type: ignore[arg-type]
            envelope,
            apply=True,
            journal=journal,
        )


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
