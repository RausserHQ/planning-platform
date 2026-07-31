"""Pure, deterministic translation from a plan and snapshot to operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from .models import BacklogItem, BacklogPlan
from .openproject import (
    OpenProjectSnapshot,
    WorkPackageSnapshot,
    create_fields,
    managed_fields,
    managed_hash,
)

OperationKind = Literal[
    "create_work_package",
    "update_managed_fields",
    "set_parent",
    "create_relation",
    "remove_managed_relation",
    "reactivate_work_package",
    "mark_superseded",
    "record_audit",
]
Identity = tuple[str, str]
ManagedRelation = tuple[str, Identity]


@dataclass(frozen=True)
class PublicationOperation:
    operation_id: str
    kind: OperationKind
    identity: Identity
    preconditions: dict[str, Any]
    managed_before_hash: str | None
    managed_after_hash: str | None
    trace_id: str
    payload: dict[str, Any]


def _preconditions(
    plan: BacklogPlan, snapshot: OpenProjectSnapshot, expected_hash: str | None
) -> dict[str, Any]:
    return {
        "publication_identity": plan.plan.publication_identity,
        "plan_version": plan.plan.version,
        "base_snapshot_sha256": snapshot.sha256,
        "base_snapshot_etag": snapshot.etag,
        "expected_managed_hash": expected_hash,
    }


def _operation_id(
    plan: BacklogPlan,
    kind: OperationKind,
    identity: Identity,
    payload: dict[str, Any],
    preconditions: dict[str, Any],
) -> str:
    raw = json.dumps(
        [plan.plan.publication_identity, plan.plan.version, kind, identity, payload, preconditions],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _operation(
    plan: BacklogPlan,
    kind: OperationKind,
    identity: Identity,
    *,
    before: str | None,
    after: str | None,
    trace_id: str,
    payload: dict[str, Any],
    preconditions: dict[str, Any],
) -> PublicationOperation:
    return PublicationOperation(
        _operation_id(plan, kind, identity, payload, preconditions),
        kind,
        identity,
        preconditions,
        before,
        after,
        trace_id,
        payload,
    )


def _relations(plan: BacklogPlan, item: BacklogItem) -> set[ManagedRelation]:
    plan_id = plan.plan.id
    relations: set[ManagedRelation] = set()
    relations.update(("blocked_by", (plan_id, target)) for target in item.blocked_by)
    relations.update(("sequence_after", (plan_id, target)) for target in item.sequence_after)
    # OpenProject permits only one relation for an endpoint pair.  The lower
    # identity owns it globally, irrespective of which endpoint declared the
    # one-sided relation.  This retains a valid `z related_to a` declaration
    # while making a two-sided declaration converge to one native relation.
    related_pairs = {
        tuple(sorted((source.key, target))) for source in plan.items for target in source.related_to
    }
    relations.update(
        ("related_to", (plan_id, target)) for source, target in related_pairs if source == item.key
    )
    relations.update(("decision_required", (plan_id, target)) for target in item.decision_required)
    relations.update(("governed_by", (plan_id, target)) for target in item.decisions)
    return relations


def _relation_payload(relation: ManagedRelation) -> dict[str, Any]:
    relation_type, target = relation
    return {"type": relation_type, "target_identity": target}


def _existing_preconditions(
    plan: BacklogPlan,
    snapshot: OpenProjectSnapshot,
    package: WorkPackageSnapshot,
    expected: str | None,
) -> dict[str, Any]:
    conditions = _preconditions(plan, snapshot, expected)
    conditions["identity_resolved"] = True
    conditions.update(_topology_preconditions(package))
    return conditions


def _topology_preconditions(
    package: WorkPackageSnapshot | None,
    *,
    parent_identity: Identity | None | object = ...,  # ... means observe package
    managed_relations: set[ManagedRelation] | None = None,
) -> dict[str, Any]:
    """Record planning topology separately from managed content.

    An unmanaged native parent is intentionally distinct from no parent: both
    have no planning identity, but replacing either blindly would discard a
    concurrent human topology edit.
    """
    if parent_identity is ...:
        assert package is not None
        parent_identity = package.parent_identity
    if managed_relations is None:
        assert package is not None
        managed_relations = set(package.managed_relations)
    return {
        "expected_parent_identity": parent_identity,
        "expected_unmanaged_parent": bool(
            package is not None
            and package.parent_id is not None
            and package.parent_identity is None
        ),
        "expected_managed_relations": [list(value) for value in sorted(managed_relations)],
    }


def plan_diff(
    plan: BacklogPlan, snapshot: OpenProjectSnapshot, trace_id: str = "local"
) -> tuple[PublicationOperation, ...]:
    """Plan create, then hierarchy, then managed planning relation mutations.

    Every relation target is the durable plan identity, never an OpenProject numeric ID.
    Adapters resolve fresh IDs and lock versions immediately before each mutation.
    """
    existing = snapshot.identities()
    desired_keys = {item.key for item in plan.items}
    operations: list[PublicationOperation] = []

    # First pass: all nodes exist before hierarchy or relation references are resolved.
    for item in sorted(plan.items, key=lambda item: item.key):
        identity = (plan.plan.id, item.key)
        if identity in existing:
            continue
        after = managed_hash(plan, item)
        conditions = _preconditions(plan, snapshot, None)
        conditions["identity_absent"] = True
        operations.append(
            _operation(
                plan,
                "create_work_package",
                identity,
                before=None,
                after=after,
                trace_id=trace_id,
                payload=create_fields(plan, item),
                preconditions=conditions,
            )
        )

    for item in sorted(plan.items, key=lambda item: item.key):
        identity = (plan.plan.id, item.key)
        package = existing.get(identity)
        after = managed_hash(plan, item)
        initial_hash = None if package is None else package.managed_hash
        if package is not None and package.superseded:
            operations.append(
                _operation(
                    plan,
                    "reactivate_work_package",
                    identity,
                    before=initial_hash,
                    after=initial_hash,
                    trace_id=trace_id,
                    payload={"status": "Ready"},
                    preconditions=_existing_preconditions(plan, snapshot, package, initial_hash),
                )
            )
        if package is not None and package.managed_hash != after:
            operations.append(
                _operation(
                    plan,
                    "update_managed_fields",
                    identity,
                    before=initial_hash,
                    after=after,
                    trace_id=trace_id,
                    payload=managed_fields(plan, item),
                    preconditions=_existing_preconditions(plan, snapshot, package, initial_hash),
                )
            )

        desired_parent = None if item.parent is None else (plan.plan.id, item.parent)
        current_parent = None if package is None else package.parent_identity
        parent_changed = current_parent != desired_parent or (
            desired_parent is None and package is not None and package.parent_id is not None
        )
        if parent_changed:
            if package is None:
                conditions = _preconditions(plan, snapshot, after)
                conditions["identity_resolved"] = True
            else:
                conditions = _existing_preconditions(plan, snapshot, package, after)
            operations.append(
                _operation(
                    plan,
                    "set_parent",
                    identity,
                    before=after,
                    after=after,
                    trace_id=trace_id,
                    payload={"parent_identity": desired_parent},
                    preconditions={
                        **conditions,
                        **_topology_preconditions(
                            package,
                            parent_identity=current_parent,
                            managed_relations=(
                                set() if package is None else set(package.managed_relations)
                            ),
                        ),
                    },
                )
            )
        # Later mutations observe the hierarchy as it will exist after this
        # deterministic parent operation, not the stale input snapshot.
        virtual_parent = desired_parent if parent_changed else current_parent

        desired_relations = _relations(plan, item)
        current_relations: set[ManagedRelation] = (
            set() if package is None else set(package.managed_relations)
        )
        if package is None:
            relation_conditions = _preconditions(plan, snapshot, after)
            relation_conditions["identity_resolved"] = True
            virtual_relations: set[ManagedRelation] = set()
        else:
            relation_conditions = _existing_preconditions(plan, snapshot, package, after)
            virtual_relations = set(package.managed_relations)
        for relation in sorted(desired_relations - current_relations):
            conditions = {
                **relation_conditions,
                **_topology_preconditions(
                    package,
                    parent_identity=virtual_parent,
                    managed_relations=virtual_relations,
                ),
            }
            operations.append(
                _operation(
                    plan,
                    "create_relation",
                    identity,
                    before=after,
                    after=after,
                    trace_id=trace_id,
                    payload=_relation_payload(relation),
                    preconditions=conditions,
                )
            )
            virtual_relations.add(relation)
        for relation in sorted(current_relations - desired_relations):
            conditions = {
                **relation_conditions,
                **_topology_preconditions(
                    package,
                    parent_identity=virtual_parent,
                    managed_relations=virtual_relations,
                ),
            }
            operations.append(
                _operation(
                    plan,
                    "remove_managed_relation",
                    identity,
                    before=after,
                    after=after,
                    trace_id=trace_id,
                    payload=_relation_payload(relation),
                    preconditions=conditions,
                )
            )
            virtual_relations.remove(relation)

    for identity, package in sorted(existing.items()):
        if identity[0] != plan.plan.id or identity[1] in desired_keys or package.superseded:
            continue
        operations.append(
            _operation(
                plan,
                "mark_superseded",
                identity,
                before=package.managed_hash,
                after=package.managed_hash,
                trace_id=trace_id,
                payload={"superseded": True},
                preconditions=_existing_preconditions(
                    plan, snapshot, package, package.managed_hash
                ),
            )
        )
    if operations:
        audit_identity = (plan.plan.id, "_audit")
        conditions = _preconditions(plan, snapshot, None)
        operations.append(
            _operation(
                plan,
                "record_audit",
                audit_identity,
                before=None,
                after=None,
                trace_id=trace_id,
                payload={"operation_ids": [operation.operation_id for operation in operations]},
                preconditions=conditions,
            )
        )
    return tuple(operations)
