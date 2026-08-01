"""Typed representation of the frozen v1 backlog contract."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STABLE_NODE_KEY_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
StableNodeKey = Annotated[
    str,
    Field(min_length=3, max_length=96, pattern=STABLE_NODE_KEY_PATTERN),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Repository(ContractModel):
    name: str
    commit: str


class SourceIdea(ContractModel):
    work_package_id: int
    lock_version: int
    updated_at: str


class SnapshotReference(ContractModel):
    captured_at: str
    etag: str
    sha256: str


class ReplanNodeBinding(ContractModel):
    """Managed-field provenance retained for one protected prior node."""

    node_key: StableNodeKey
    plan_version: int = Field(ge=1)
    planning_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ReplanScope(ContractModel):
    """Artifact-visible boundary and provenance for a partial replan."""

    base_plan_version: int = Field(ge=1)
    selected_root_keys: tuple[StableNodeKey, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    affected_node_keys: tuple[StableNodeKey, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    retained_node_bindings: tuple[ReplanNodeBinding, ...] = Field(
        json_schema_extra={"uniqueItems": True},
    )

    @model_validator(mode="after")
    def identities_are_unique(self) -> ReplanScope:
        if len(self.selected_root_keys) != len(set(self.selected_root_keys)):
            raise ValueError("replan selected root keys must be unique")
        if len(self.affected_node_keys) != len(set(self.affected_node_keys)):
            raise ValueError("replan affected node keys must be unique")
        binding_keys = [binding.node_key for binding in self.retained_node_bindings]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("replan retained node bindings must be unique")
        return self


class Plan(ContractModel):
    id: str
    version: int
    publication_identity: str
    source_idea: SourceIdea
    repositories: tuple[Repository, ...]
    approved_planning_commit: str | None
    openproject_snapshot: SnapshotReference
    replan: ReplanScope | None = None


class AcceptanceCriterion(ContractModel):
    criterion: str
    observation: str


class RequiredEvidence(ContractModel):
    kind: Literal[
        "artifact",
        "check_run",
        "command_output",
        "decision_record",
        "deployment",
        "metric",
        "pull_request",
        "review",
    ]
    description: str


class ResultPredicate(ContractModel):
    kind: Literal["artifact", "command", "deployment", "github_check", "metric"]
    expression: str


class AgentEligibility(ContractModel):
    eligible: bool
    reason: str


ItemType = Literal["Initiative", "Epic", "Story", "Task", "Decision", "Investigation", "Bug"]


class BacklogItem(ContractModel):
    key: str
    type: ItemType
    title: str
    objective: str
    parent: str | None
    repository: str
    integration_work: bool
    source_requirements: tuple[str, ...]
    maintenance_objectives: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    blocked_by: tuple[str, ...]
    sequence_after: tuple[str, ...]
    related_to: tuple[str, ...]
    decision_required: tuple[str, ...]
    decisions: tuple[str, ...]
    mutex: tuple[str, ...]
    risk: Literal["low", "medium", "high", "critical"]
    estimate: Literal["XS", "S", "M"]
    agent_eligibility: AgentEligibility
    validation_commands: tuple[str, ...]
    required_evidence: tuple[RequiredEvidence, ...]
    result_predicate: ResultPredicate


class BacklogPlan(ContractModel):
    schema_version: Literal["1.0.0"]
    plan: Plan
    items: tuple[BacklogItem, ...]

    @property
    def by_key(self) -> dict[str, BacklogItem]:
        return {item.key: item for item in self.items}


def with_approved_commit(plan: BacklogPlan, approved_commit: str) -> BacklogPlan:
    """Materialize the merge commit in memory without changing the approved artifact bytes."""
    return plan.model_copy(
        update={"plan": plan.plan.model_copy(update={"approved_planning_commit": approved_commit})}
    )
