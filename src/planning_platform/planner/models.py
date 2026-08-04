"""API and structured-model contracts for the private planner."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from planning_platform.models import BacklogItem, BacklogPlan, StableNodeKey

MAX_PLANNER_REQUEST_BYTES = 4 * 1024 * 1024
MAX_INTERRUPT_ITEMS = 16
InterruptText = Annotated[str, Field(min_length=1, max_length=2_048)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryFile(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=262_144)

    @model_validator(mode="after")
    def content_matches_hash(self) -> RepositoryFile:
        actual = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if actual != self.sha256:
            raise ValueError("repository file content does not match sha256")
        return self


class RepositorySnapshot(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[RepositoryFile, ...] = Field(max_length=500)

    @model_validator(mode="after")
    def snapshot_matches_hash(self) -> RepositorySnapshot:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("repository snapshot contains duplicate file paths")
        if repository_snapshot_digest(self.name, self.commit, self.files) != self.snapshot_sha256:
            raise ValueError("repository snapshot does not match snapshot_sha256")
        return self


class PlannerEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    idempotency_key: str = Field(min_length=8, max_length=255)
    trace_id: UUID


class IdeaSnapshot(StrictModel):
    work_package_id: int = Field(ge=1)
    lock_version: int = Field(ge=0)
    updated_at: AwareDatetime
    title: str = Field(min_length=1)
    description: str = Field(default="", max_length=65_536)


class OpenProjectSnapshotInput(StrictModel):
    captured_at: AwareDatetime
    etag: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplanContext(StrictModel):
    """Immutable prior plan and the exact subtree an operator authorized to change."""

    prior_plan: BacklogPlan
    base_approved_planning_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    selected_root_keys: tuple[StableNodeKey, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    affected_node_keys: tuple[StableNodeKey, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    reason: str = Field(min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def selected_roots_define_the_exact_prior_closure(self) -> ReplanContext:
        prior = self.prior_plan.by_key
        roots = self.selected_root_keys
        if len(roots) != len(set(roots)) or any(key not in prior for key in roots):
            raise ValueError("replan selected roots must be unique prior node keys")
        if len(self.affected_node_keys) != len(set(self.affected_node_keys)):
            raise ValueError("replan affected node keys must be unique")
        for root in roots:
            current = prior[root].parent
            seen = {root}
            while current is not None and current not in seen:
                if current in roots:
                    raise ValueError("replan selected roots must not be nested")
                seen.add(current)
                item = prior.get(current)
                current = None if item is None else item.parent
        expected = _descendant_closure(self.prior_plan, set(roots))
        if set(self.affected_node_keys) != expected:
            raise ValueError(
                "replan affected node keys must equal the selected-root descendant closure"
            )
        return self


class StartPlanRequest(StrictModel):
    event: PlannerEvent
    idea: IdeaSnapshot
    plan_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    plan_version: int = Field(ge=1)
    idea_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    openproject_snapshot: OpenProjectSnapshotInput
    repositories: tuple[RepositorySnapshot, ...] = Field(min_length=1, max_length=20)
    replan: ReplanContext | None = None

    @model_validator(mode="after")
    def idea_matches_hash(self) -> StartPlanRequest:
        actual = idea_snapshot_digest(self.idea, self.openproject_snapshot)
        if actual != self.idea_sha256:
            raise ValueError("Idea fields do not match idea_sha256")
        if self.replan is not None:
            prior = self.replan.prior_plan.plan
            if (
                prior.id != self.plan_id
                or prior.version >= self.plan_version
                or prior.source_idea.work_package_id != self.idea.work_package_id
            ):
                raise ValueError("replan context does not bind an earlier plan for this Idea")
        _validate_request_size(self)
        return self


class ResumePlanRequest(StrictModel):
    event: PlannerEvent
    interrupt_id: str = Field(min_length=1)
    comment_id: int = Field(ge=1)
    comment_created_at: AwareDatetime
    answer: str = Field(min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def request_is_bounded(self) -> ResumePlanRequest:
        _validate_request_size(self)
        return self


class AbandonTerminalResumeRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=255)
    interrupt_id: str = Field(min_length=1, max_length=255)
    comment_id: int = Field(ge=1)
    comment_created_at: AwareDatetime
    operator: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=1_024)


class ConvertFailedCritiqueRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=255)
    interrupt_id: str = Field(min_length=1, max_length=255)
    comment_id: int = Field(ge=1)
    comment_created_at: AwareDatetime
    operator: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,254}$")
    reason: str = Field(min_length=1, max_length=1_024)

    @model_validator(mode="after")
    def reason_is_plain_text(self) -> ConvertFailedCritiqueRequest:
        if any(value in self.reason for value in ("\r", "\n", "{", "}")):
            raise ValueError("critique correction reason must be bounded plain text")
        return self


class ArtifactManifestEntry(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required: bool = True


class PendingInterrupt(StrictModel):
    interrupt_id: str
    questions: tuple[InterruptText, ...] = Field(max_length=MAX_INTERRUPT_ITEMS)
    impact: str = Field(min_length=1, max_length=1_024)
    created_at: AwareDatetime


class PlanResponse(StrictModel):
    thread_id: str
    status: Literal["planning", "needs_input", "artifacts_ready", "failed"]
    trace_id: str
    interrupt: PendingInterrupt | None
    artifact_manifest: tuple[ArtifactManifestEntry, ...]


class ArtifactContent(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str


class ArtifactBundle(StrictModel):
    thread_id: str
    artifacts: tuple[ArtifactContent, ...]


class ScopeClassification(StrictModel):
    scope: Literal["tiny", "material"]
    risk: Literal["low", "medium", "high", "critical"]
    rationale: str


class CompactSpecification(StrictModel):
    problem: str
    desired_outcome: str
    constraints: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()


class ConsequentialQuestions(StrictModel):
    questions: tuple[InterruptText, ...] = Field(default=(), max_length=MAX_INTERRUPT_ITEMS)
    impact: str = Field(max_length=1_024)


class DocumentSection(StrictModel):
    title: str
    body: str


class RequirementsDraft(StrictModel):
    requirements: tuple[str, ...]
    decisions: tuple[str, ...] = ()
    items: tuple[BacklogItem, ...]


class DecompositionDraft(StrictModel):
    items: tuple[BacklogItem, ...]


class RelationDraft(StrictModel):
    items: tuple[BacklogItem, ...]


class DecompositionCritique(StrictModel):
    acceptable: bool
    findings: tuple[InterruptText, ...] = Field(default=(), max_length=MAX_INTERRUPT_ITEMS)


class ResumeBinding(StrictModel):
    interrupt_id: str
    comment_id: int
    comment_created_at: AwareDatetime


def derive_thread_id(idea_id: int, plan_version: int) -> str:
    return f"openproject:{idea_id}:planning:{plan_version}"


def repository_snapshot_digest(name: str, commit: str, files: tuple[RepositoryFile, ...]) -> str:
    value = {
        "name": name,
        "commit": commit,
        "files": [
            {"path": file.path, "sha256": file.sha256}
            for file in sorted(files, key=lambda candidate: candidate.path)
        ],
    }
    return _canonical_sha256(value)


def idea_snapshot_digest(idea: IdeaSnapshot, snapshot: OpenProjectSnapshotInput) -> str:
    return _canonical_sha256(
        {
            "idea": idea.model_dump(mode="json"),
            "openproject_snapshot": snapshot.model_dump(mode="json"),
        }
    )


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_request_size(request: BaseModel) -> None:
    size = len(request.model_dump_json().encode("utf-8"))
    if size > MAX_PLANNER_REQUEST_BYTES:
        raise ValueError(f"planner request exceeds {MAX_PLANNER_REQUEST_BYTES} UTF-8 bytes")


def _descendant_closure(plan: BacklogPlan, roots: set[str]) -> set[str]:
    closure = set(roots)
    while True:
        expanded = closure | {item.key for item in plan.items if item.parent in closure}
        if expanded == closure:
            return closure
        closure = expanded
