"""Semantic rules which deliberately remain outside JSON Schema."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .loader import SchemaValidationError, validate_schema
from .models import BacklogItem, BacklogPlan


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    node_key: str | None = None


class SemanticValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.code}: {issue.message}" for issue in self.issues))


_REFERENCES = (
    "parent",
    "blocked_by",
    "sequence_after",
    "related_to",
    "decision_required",
    "decisions",
)
_EXECUTABLE = {"Story", "Task", "Investigation", "Bug"}
_RANK = {
    "Initiative": 0,
    "Epic": 1,
    "Story": 2,
    "Task": 3,
    "Decision": 3,
    "Investigation": 3,
    "Bug": 3,
}
_VAGUE = {"tbd", "n/a", "manual", "done", "success", "successful", "objective", "as desired"}


def _references(item: BacklogItem) -> Iterable[tuple[str, str]]:
    if item.parent:
        yield "parent", item.parent
    for field in _REFERENCES[1:]:
        for key in getattr(item, field):
            yield field, key


def _contains_vague(value: str) -> bool:
    lowered = value.casefold()
    return any(word in lowered for word in _VAGUE)


def _has_cycle(edges: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> bool:
        if key in visiting:
            return True
        if key in visited:
            return False
        visiting.add(key)
        if any(visit(target) for target in edges[key]):
            return True
        visiting.remove(key)
        visited.add(key)
        return False

    return any(visit(key) for key in edges)


def validate_plan(
    plan: BacklogPlan, *, raise_on_error: bool = False
) -> tuple[ValidationIssue, ...]:
    """Return deterministic violations; no external state is consulted."""
    issues: list[ValidationIssue] = []
    try:
        validate_schema(plan.model_dump(mode="json"))
    except SchemaValidationError as error:
        issues.append(ValidationIssue("schema_contract", str(error)))
    keys = [item.key for item in plan.items]
    known = set(keys)
    repositories = {repository.name for repository in plan.plan.repositories}
    if plan.plan.publication_identity != f"{plan.plan.id}:v{plan.plan.version}":
        issues.append(ValidationIssue("publication_identity", "must equal plan id and version"))
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        issues.append(ValidationIssue("duplicate_key", f"duplicate node key {key}", key))

    parent_edges = {
        item.key: {item.parent} if item.parent in known else set() for item in plan.items
    }
    dependency_edges = {
        item.key: {key for key in item.blocked_by if key in known} for item in plan.items
    }
    for item in plan.items:
        relation_fields = {
            "blocked_by": item.blocked_by,
            "sequence_after": item.sequence_after,
            "related_to": item.related_to,
            "decision_required": item.decision_required,
            "decisions": item.decisions,
        }
        relation_uses: dict[str, list[str]] = {}
        for field, targets in relation_fields.items():
            for target in targets:
                relation_uses.setdefault(target, []).append(field)
        for target, fields in sorted(relation_uses.items()):
            if len(fields) > 1:
                issues.append(
                    ValidationIssue(
                        "conflicting_relation_semantics",
                        f"{target} is referenced by multiple relation kinds: {', '.join(fields)}",
                        item.key,
                    )
                )
        if item.repository not in repositories:
            issues.append(
                ValidationIssue(
                    "unknown_repository", "repository is not in plan.repositories", item.key
                )
            )
        for relation, target in _references(item):
            if target not in known:
                issues.append(
                    ValidationIssue(
                        "dangling_reference", f"{relation} references {target}", item.key
                    )
                )
                continue
            if target == item.key:
                issues.append(
                    ValidationIssue(
                        "self_reference", f"{relation} cannot reference itself", item.key
                    )
                )
            target_item = plan.by_key[target]
            if relation in {"decision_required", "decisions"} and target_item.type != "Decision":
                issues.append(
                    ValidationIssue(
                        "invalid_decision_target",
                        f"{relation} must reference a Decision item",
                        item.key,
                    )
                )
            if relation == "parent":
                if _RANK[target_item.type] >= _RANK[item.type]:
                    issues.append(
                        ValidationIssue(
                            "meaningless_parent", "parent must be a broader work item", item.key
                        )
                    )
                if target_item.repository != item.repository:
                    issues.append(
                        ValidationIssue(
                            "cross_repository_parent",
                            "parent must be in the same repository",
                            item.key,
                        )
                    )
            elif target_item.repository != item.repository and not item.integration_work:
                issues.append(
                    ValidationIssue(
                        "cross_repository_relation",
                        "cross-repository relation requires integration_work",
                        item.key,
                    )
                )
        if item.type in _EXECUTABLE and not (
            item.source_requirements or item.maintenance_objectives
        ):
            issues.append(
                ValidationIssue(
                    "traceability",
                    "executable item needs a requirement or maintenance objective",
                    item.key,
                )
            )
        for trace in (*item.source_requirements, *item.maintenance_objectives):
            if trace.casefold().strip() in {"other", "catch-all", "catch all", "miscellaneous"}:
                issues.append(
                    ValidationIssue(
                        "catch_all_traceability", "catch-all traceability is not allowed", item.key
                    )
                )
        for criterion in item.acceptance_criteria:
            if _contains_vague(criterion.observation):
                issues.append(
                    ValidationIssue(
                        "unobservable_criterion",
                        "observation must name an observable proof",
                        item.key,
                    )
                )
            if " and " in criterion.criterion.casefold():
                issues.append(
                    ValidationIssue(
                        "multi_outcome_criterion",
                        "split acceptance criteria into one outcome",
                        item.key,
                    )
                )
        if not item.required_evidence:
            issues.append(
                ValidationIssue("missing_evidence", "required evidence is mandatory", item.key)
            )
        if _contains_vague(item.result_predicate.expression):
            issues.append(
                ValidationIssue(
                    "non_objective_predicate", "result predicate is not machine-checkable", item.key
                )
            )
        if any(" " in blocker.strip() for blocker in item.blocked_by):
            issues.append(
                ValidationIssue("plain_text_blocker", "blocked_by only accepts node keys", item.key)
            )
        if item.estimate not in {"XS", "S", "M"}:
            issues.append(
                ValidationIssue("unsupported_estimate", "only XS, S, and M are supported", item.key)
            )
    if _has_cycle(parent_edges):
        issues.append(ValidationIssue("parent_cycle", "parent hierarchy contains a cycle"))
    if _has_cycle(dependency_edges):
        issues.append(ValidationIssue("dependency_cycle", "blocked_by graph contains a cycle"))
    result = tuple(issues)
    if raise_on_error and result:
        raise SemanticValidationError(result)
    return result
