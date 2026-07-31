"""OpenProject snapshot types and managed-field hashing.

The types intentionally retain human fields separately; publishers never put
them into a managed payload or hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import BacklogItem, BacklogPlan


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class WorkPackageSnapshot:
    id: int
    lock_version: int
    plan_id: str | None
    node_key: str | None
    plan_version: int | None = None
    title: str = ""
    managed_hash: str | None = None
    parent_id: int | None = None
    parent_identity: tuple[str, str] | None = None
    relations: tuple[tuple[str, int], ...] = ()
    managed_relations: tuple[tuple[str, tuple[str, str]], ...] = ()
    human_fields: dict[str, Any] = field(default_factory=dict)
    superseded: bool = False
    evidence_state: str | None = None

    @property
    def identity(self) -> tuple[str, str] | None:
        if self.plan_id is None or self.node_key is None:
            return None
        return self.plan_id, self.node_key

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkPackageSnapshot:
        relations = tuple(
            (str(relation["type"]), int(relation["to_id"]))
            for relation in value.get("relations", [])
        )
        managed_relations = tuple(
            (
                str(relation["type"]),
                (str(relation["target_plan_id"]), str(relation["target_node_key"])),
            )
            for relation in value.get("managed_relations", [])
        )
        parent = value.get("parent_identity")
        parent_identity = (
            None if parent is None else (str(parent["plan_id"]), str(parent["node_key"]))
        )
        return cls(
            id=int(value["id"]),
            lock_version=int(value.get("lock_version", 0)),
            plan_id=value.get("plan_id"),
            node_key=value.get("node_key"),
            plan_version=value.get("plan_version"),
            title=value.get("title", ""),
            managed_hash=value.get("managed_hash"),
            parent_id=value.get("parent_id"),
            parent_identity=parent_identity,
            relations=relations,
            managed_relations=managed_relations,
            human_fields=dict(value.get("human_fields", {})),
            superseded=bool(value.get("superseded", False)),
            evidence_state=value.get("evidence_state"),
        )


@dataclass(frozen=True)
class OpenProjectSnapshot:
    captured_at: str
    etag: str
    sha256: str
    work_packages: tuple[WorkPackageSnapshot, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OpenProjectSnapshot:
        return cls(
            captured_at=str(value["captured_at"]),
            etag=str(value["etag"]),
            sha256=str(value["sha256"]),
            work_packages=tuple(
                WorkPackageSnapshot.from_dict(package) for package in value.get("work_packages", [])
            ),
        )

    def identities(self) -> dict[tuple[str, str], WorkPackageSnapshot]:
        result: dict[tuple[str, str], WorkPackageSnapshot] = {}
        for package in self.work_packages:
            if package.identity is not None:
                if package.identity in result:
                    raise ValueError(
                        f"conflicting identity {package.identity[0]}:{package.identity[1]}"
                    )
                result[package.identity] = package
        return result

    def content_hash(self) -> str:
        packages = [asdict(package) for package in self.work_packages]
        return canonical_hash(
            {"captured_at": self.captured_at, "etag": self.etag, "work_packages": packages}
        )


def generated_description(item: BacklogItem) -> str:
    criteria = "\n".join(
        f"- {value.criterion}\n  Proof: {value.observation}" for value in item.acceptance_criteria
    )
    evidence = "\n".join(f"- {value.kind}: {value.description}" for value in item.required_evidence)
    sections = (
        "<!-- planning-platform:generated -->",
        "## Type",
        item.type,
        "## Objective",
        item.objective,
        "## Acceptance",
        criteria,
        "## Evidence",
        evidence,
        "## Estimate",
        item.estimate,
        "## Repository",
        item.repository,
        "## Source requirements",
        "\n".join(f"- {value}" for value in item.source_requirements),
        "## Maintenance objectives",
        "\n".join(f"- {value}" for value in item.maintenance_objectives),
        "## Agent eligibility rationale",
        item.agent_eligibility.reason,
        "<!-- /planning-platform:generated -->",
    )
    return "\n".join(sections)


_GENERATED_OPEN = "<!-- planning-platform:generated -->"
_GENERATED_CLOSE = "<!-- /planning-platform:generated -->"


def replace_generated_description(current: str, generated: str) -> str:
    """Replace only the bounded publisher-owned region of a description.

    A malformed or multiply-owned region is unsafe: choosing one would risk
    overwriting a human edit, so callers must stop instead.
    """
    starts = current.count(_GENERATED_OPEN)
    ends = current.count(_GENERATED_CLOSE)
    if starts == 0 and ends == 0:
        return generated if not current.strip() else f"{current.rstrip()}\n\n{generated}"
    if starts != 1 or ends != 1:
        raise ValueError("generated description markers are missing, duplicated, or unbalanced")
    start = current.index(_GENERATED_OPEN)
    end = current.index(_GENERATED_CLOSE, start) + len(_GENERATED_CLOSE)
    if end <= start:
        raise ValueError("generated description marker order is invalid")
    return f"{current[:start]}{generated}{current[end:]}"


def managed_fields(plan: BacklogPlan, item: BacklogItem) -> dict[str, Any]:
    """Canonical v1 observable publisher-owned projection.

    Every member is either a native v3 field/custom field or the exact bounded
    generated region, so an adapter can recompute this hash from a fresh read.
    """
    return {
        "title": item.title,
        "work_package_type": item.type,
        "generated_description": generated_description(item),
        "priority": item.risk,
        "risk": item.risk,
        "repository": item.repository,
        "source_requirements": list(item.source_requirements),
        "plan_id": plan.plan.id,
        "node_key": item.key,
        "plan_version": plan.plan.version,
        "agent_eligibility": item.agent_eligibility.eligible,
        "planning_commit": plan.plan.approved_planning_commit,
    }


def create_fields(plan: BacklogPlan, item: BacklogItem) -> dict[str, Any]:
    """Publisher-owned fields plus lifecycle defaults used only at creation."""
    return {
        **managed_fields(plan, item),
        "evidence_state": "pending",
    }


def managed_hash(plan: BacklogPlan, item: BacklogItem) -> str:
    return canonical_hash(managed_fields(plan, item))
