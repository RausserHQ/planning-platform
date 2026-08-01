"""Deterministic partial-replan boundary shared by planner and lifecycle."""

from __future__ import annotations

from collections.abc import Iterable

from planning_platform.models import (
    BacklogItem,
    BacklogPlan,
    ReplanNodeBinding,
    ReplanScope,
)

_DIRECTED_RELATIONS = (
    "blocked_by",
    "sequence_after",
    "decision_required",
    "decisions",
    "mutex",
)


def effective_node_binding(
    plan: BacklogPlan,
    approved_commit: str | None,
    node_key: str,
) -> tuple[int, str | None]:
    """Return the OpenProject version/commit owned by one node in a mixed-version plan."""
    scope = plan.plan.replan
    if scope is not None:
        for binding in scope.retained_node_bindings:
            if binding.node_key == node_key:
                return binding.plan_version, binding.planning_commit
    return plan.plan.version, approved_commit


def build_replan_scope(
    prior: BacklogPlan,
    *,
    base_approved_commit: str,
    selected_root_keys: Iterable[str],
    affected_node_keys: Iterable[str],
) -> ReplanScope:
    """Carry exact per-node provenance for every node protected by this replan."""
    selected = tuple(selected_root_keys)
    affected = tuple(affected_node_keys)
    protected = set(prior.by_key) - set(affected)
    bindings = []
    for item in prior.items:
        if item.key not in protected:
            continue
        version, commit = effective_node_binding(prior, base_approved_commit, item.key)
        if commit is None:
            raise ValueError("protected replan node has no approved planning commit")
        bindings.append(
            ReplanNodeBinding(
                node_key=item.key,
                plan_version=version,
                planning_commit=commit,
            )
        )
    return ReplanScope(
        base_plan_version=prior.plan.version,
        selected_root_keys=selected,
        affected_node_keys=affected,
        retained_node_bindings=tuple(bindings),
    )


def apply_replan_boundary(
    prior: BacklogPlan,
    proposed: BacklogPlan,
    *,
    base_approved_commit: str,
    selected_root_keys: Iterable[str],
    affected_node_keys: Iterable[str],
) -> BacklogPlan:
    """Overlay protected nodes and attach the exact artifact-visible replan scope."""
    selected = tuple(selected_root_keys)
    affected = tuple(affected_node_keys)
    proposed_keys = [item.key for item in proposed.items]
    if len(proposed_keys) != len(set(proposed_keys)):
        raise ValueError("replan candidate contains duplicate node keys")
    proposed_by_key = proposed.by_key
    if any(key not in proposed_by_key for key in selected):
        raise ValueError("replan removed a selected root")

    affected_set = set(affected)
    merged = []
    for item in prior.items:
        if item.key not in affected_set:
            merged.append(item)
        elif item.key in proposed_by_key:
            merged.append(proposed_by_key[item.key])
    merged.extend(item for item in proposed.items if item.key not in prior.by_key)
    scope = build_replan_scope(
        prior,
        base_approved_commit=base_approved_commit,
        selected_root_keys=selected,
        affected_node_keys=affected,
    )
    bounded = proposed.model_copy(
        update={
            "plan": proposed.plan.model_copy(update={"replan": scope}),
            "items": tuple(merged),
        }
    )
    validate_replan_candidate(
        prior,
        bounded,
        base_approved_commit=base_approved_commit,
        selected_root_keys=selected,
        affected_node_keys=affected,
    )
    return bounded


def validate_replan_candidate(
    prior: BacklogPlan,
    candidate: BacklogPlan,
    *,
    base_approved_commit: str,
    selected_root_keys: Iterable[str],
    affected_node_keys: Iterable[str],
) -> None:
    """Reject any candidate that crosses or rewrites the operator-selected boundary."""
    selected = tuple(selected_root_keys)
    affected = tuple(affected_node_keys)
    roots = set(selected)
    closure = set(affected)
    prior_by_key = prior.by_key
    candidate_by_key = candidate.by_key
    if (
        candidate.plan.id != prior.plan.id
        or candidate.plan.version <= prior.plan.version
        or candidate.plan.source_idea.work_package_id != prior.plan.source_idea.work_package_id
    ):
        raise ValueError("replan candidate does not descend from its immutable base plan")
    expected_scope = build_replan_scope(
        prior,
        base_approved_commit=base_approved_commit,
        selected_root_keys=selected,
        affected_node_keys=affected,
    )
    if candidate.plan.replan != expected_scope:
        raise ValueError("replan candidate has mismatched scope or retained-node bindings")
    for key, item in prior_by_key.items():
        if key not in closure and candidate_by_key.get(key) != item:
            raise ValueError(f"replan changed protected prior node: {key}")
    if any(key not in candidate_by_key for key in roots):
        raise ValueError("replan removed a selected root")
    for key in roots:
        if candidate_by_key[key].parent != prior_by_key[key].parent:
            raise ValueError(f"replan changed selected-root boundary parent: {key}")

    mutable_keys = closure | (set(candidate_by_key) - set(prior_by_key))
    for key in sorted(mutable_keys - roots):
        if key in candidate_by_key and not _reaches_selected_root(key, candidate_by_key, roots):
            raise ValueError(f"replan node is not rooted in a selected root: {key}")

    protected = set(prior_by_key) - closure
    if _cross_boundary_edges(prior, protected) != _cross_boundary_edges(candidate, protected):
        raise ValueError("replan changed a relation across the protected boundary")


def _reaches_selected_root(
    key: str,
    items: dict[str, BacklogItem],
    roots: set[str],
) -> bool:
    seen = {key}
    current = key
    while current not in roots:
        item = items.get(current)
        parent = None if item is None else item.parent
        if not isinstance(parent, str) or parent in seen or parent not in items:
            return False
        seen.add(parent)
        current = parent
    return True


def _cross_boundary_edges(
    plan: BacklogPlan,
    protected: set[str],
) -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    known = set(plan.by_key)
    for item in plan.items:
        if item.parent is not None and item.parent in known:
            _add_crossing(edges, "parent", item.key, item.parent, protected)
        for field in _DIRECTED_RELATIONS:
            for target in getattr(item, field):
                if target in known:
                    _add_crossing(edges, field, item.key, target, protected)
        for target in item.related_to:
            if target in known and ((item.key in protected) != (target in protected)):
                left, right = sorted((item.key, target))
                edges.add(("related_to", left, right))
    return edges


def _add_crossing(
    edges: set[tuple[str, str, str]],
    kind: str,
    source: str,
    target: str,
    protected: set[str],
) -> None:
    if (source in protected) != (target in protected):
        edges.add((kind, source, target))
