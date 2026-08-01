"""LangGraph planning pipeline with ephemeral repository runtime context."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from planning_platform.models import BacklogPlan
from planning_platform.replan import apply_replan_boundary
from planning_platform.validation import SemanticValidationError, validate_plan

from .artifacts import artifact_manifest, render_artifact_set
from .model import PlanningModel
from .models import (
    CompactSpecification,
    ConsequentialQuestions,
    DecompositionCritique,
    DecompositionDraft,
    DocumentSection,
    RelationDraft,
    RepositorySnapshot,
    RequirementsDraft,
    ScopeClassification,
)


class PlannerState(TypedDict, total=False):
    thread_id: str
    trace_id: str
    status: str
    plan_id: str
    plan_version: int
    idea: dict[str, Any]
    openproject_snapshot: dict[str, Any]
    repositories: list[dict[str, str]]
    replan: dict[str, Any]
    classification: dict[str, Any]
    repository_context: dict[str, Any]
    compact_specification: dict[str, Any]
    questions: list[str]
    pending_interrupt: dict[str, Any] | None
    human_answer: str
    consumed_resume: dict[str, Any]
    prd: dict[str, Any] | None
    architecture: dict[str, Any]
    requirements_draft: dict[str, Any]
    decomposition: dict[str, Any]
    relation_draft: dict[str, Any]
    critique: dict[str, Any]
    backlog: dict[str, Any]
    artifacts: dict[str, str]
    artifact_manifest: list[dict[str, Any]]


@dataclass(frozen=True)
class PlannerRuntimeContext:
    repositories: tuple[RepositorySnapshot, ...]


_SECRET = re.compile(
    r"(?i)(token|password|secret|api[_ -]?key|private[_ -]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?"
    r"-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----",
    re.DOTALL,
)
_TOKEN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
    r"\.[A-Za-z0-9_-]{10,})"
)
_HIGH_ENTROPY = re.compile(r"(?=[A-Za-z0-9+/=_-]{32,})(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9+/=_-]+")


def _sanitize_text(value: str, *, limit: int = 8000) -> str:
    value = _PEM.sub("[REDACTED PEM]", value)
    value = _SECRET.sub(r"\1=[REDACTED]", value)
    value = _TOKEN.sub("[REDACTED TOKEN]", value)
    value = _HIGH_ENTROPY.sub("[REDACTED CREDENTIAL]", value)
    return value[:limit]


def sanitize_checkpoint_text(value: str, *, limit: int = 8000) -> str:
    """Sanitize untrusted text before it can enter durable graph state."""
    return _sanitize_text(value, limit=limit)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def _model_payload(state: PlannerState) -> dict[str, Any]:
    return {
        "title": state["idea"]["title"],
        "description": state["idea"].get("description", ""),
        "classification": state.get("classification", {}),
        "repository_context": state.get("repository_context", {}),
        "human_answer": state.get("human_answer", ""),
        "compact_specification": state.get("compact_specification", {}),
        "prd": state.get("prd"),
        "architecture": state.get("architecture", {}),
        "requirements_draft": state.get("requirements_draft", {}),
        "decomposition": state.get("decomposition", {}),
        "relation_draft": state.get("relation_draft", {}),
        "replan": state.get("replan"),
    }


def build_planner_graph(model: PlanningModel, checkpointer: Any) -> Any:
    async def normalize_intake(state: PlannerState) -> dict[str, Any]:
        idea = dict(state["idea"])
        idea["title"] = _sanitize_text(str(idea["title"]))
        idea["description"] = _sanitize_text(str(idea.get("description", "")))
        return {"idea": idea, "status": "planning"}

    async def classify_scope_and_risk(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "classify_scope_and_risk", ScopeClassification, _model_payload(state)
        )
        return {"classification": _sanitize_value(result.model_dump(mode="json"))}

    async def retrieve_repository_context(
        state: PlannerState, runtime: Runtime[PlannerRuntimeContext]
    ) -> dict[str, Any]:
        repository_payload = [
            {
                "name": repository.name,
                "commit": repository.commit,
                "files": [
                    {
                        "path": file.path,
                        "sha256": file.sha256,
                        "content": file.content,
                    }
                    for file in repository.files
                ],
            }
            for repository in runtime.context.repositories
        ]
        result = await model.generate(
            "retrieve_repository_context",
            DocumentSection,
            {**_model_payload(state), "repositories": repository_payload},
        )
        return {
            "repository_context": {
                "summary": _sanitize_text(result.body),
                "files": [
                    {"repository": repository.name, "path": file.path, "sha256": file.sha256}
                    for repository in runtime.context.repositories
                    for file in repository.files
                ],
            }
        }

    async def draft_compact_specification(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "draft_compact_specification", CompactSpecification, _model_payload(state)
        )
        return {"compact_specification": _sanitize_value(result.model_dump(mode="json"))}

    async def identify_consequential_questions(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "identify_consequential_questions", ConsequentialQuestions, _model_payload(state)
        )
        questions = [_sanitize_text(question) for question in result.questions]
        if not questions:
            return {"questions": [], "pending_interrupt": None}
        impact = _sanitize_text(result.impact, limit=1_024).strip()
        if not impact:
            raise ValueError("consequential questions require a concise impact")
        digest = hashlib.sha256(f"{state['thread_id']}:{questions}:{impact}".encode()).hexdigest()[
            :24
        ]
        return {
            "questions": questions,
            "pending_interrupt": {
                "interrupt_id": digest,
                "questions": questions,
                "impact": impact,
                "created_at": datetime.now(UTC).isoformat(),
            },
        }

    async def human_interrupt_when_required(state: PlannerState) -> dict[str, Any]:
        pending = state.get("pending_interrupt")
        if not pending:
            return {}
        resumed = interrupt(pending)
        answer = resumed["answer"] if isinstance(resumed, dict) else resumed
        trace_id = (
            str(UUID(str(resumed["trace_id"]))) if isinstance(resumed, dict) else state["trace_id"]
        )
        consumed = (
            {key: resumed[key] for key in ("interrupt_id", "comment_id", "comment_created_at")}
            if isinstance(resumed, dict)
            else {}
        )
        return {
            "human_answer": _sanitize_text(str(answer)),
            "trace_id": trace_id,
            "consumed_resume": consumed,
            "pending_interrupt": None,
        }

    async def generate_prd_when_warranted(state: PlannerState) -> dict[str, Any]:
        if state["classification"]["scope"] == "tiny":
            return {"prd": None}
        result = await model.generate(
            "generate_prd_when_warranted", DocumentSection, _model_payload(state)
        )
        return {"prd": _sanitize_value(result.model_dump(mode="json"))}

    async def generate_architecture(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "generate_architecture", DocumentSection, _model_payload(state)
        )
        return {"architecture": _sanitize_value(result.model_dump(mode="json"))}

    async def derive_requirements_and_decisions(state: PlannerState) -> dict[str, Any]:
        repository = state["repositories"][0]["name"]
        result = await model.generate(
            "derive_requirements_and_decisions",
            RequirementsDraft,
            {**_model_payload(state), "repository": repository},
        )
        return {"requirements_draft": _sanitize_value(result.model_dump(mode="json"))}

    async def decompose_epics_and_stories(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "decompose_epics_and_stories",
            DecompositionDraft,
            _model_payload(state),
        )
        return {"decomposition": _sanitize_value(result.model_dump(mode="json"))}

    async def infer_typed_relations(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "infer_typed_relations",
            RelationDraft,
            _model_payload(state),
        )
        return {"relation_draft": _sanitize_value(result.model_dump(mode="json"))}

    async def critique_decomposition(state: PlannerState) -> dict[str, Any]:
        result = await model.generate(
            "critique_decomposition", DecompositionCritique, _model_payload(state)
        )
        critique = cast(dict[str, Any], _sanitize_value(result.model_dump(mode="json")))
        if not critique["acceptable"]:
            findings = tuple(str(finding) for finding in critique["findings"])
            raise ValueError(f"decomposition critique failed: {findings}")
        return {"critique": critique}

    async def validate_backlog(state: PlannerState) -> dict[str, Any]:
        declared_requirements = set(state["requirements_draft"]["requirements"])
        relation_items = state["relation_draft"]["items"]
        replan = state.get("replan")
        backlog = BacklogPlan.model_validate(
            {
                "schema_version": "1.0.0",
                "plan": {
                    "id": state["plan_id"],
                    "version": state["plan_version"],
                    "publication_identity": (f"{state['plan_id']}:v{state['plan_version']}"),
                    "source_idea": {
                        "work_package_id": state["idea"]["work_package_id"],
                        "lock_version": state["idea"]["lock_version"],
                        "updated_at": state["idea"]["updated_at"],
                    },
                    "repositories": state["repositories"],
                    "approved_planning_commit": None,
                    "openproject_snapshot": state["openproject_snapshot"],
                },
                "items": relation_items,
            }
        )
        mutable_keys = set(backlog.by_key)
        if isinstance(replan, dict):
            prior = BacklogPlan.model_validate(replan.get("prior_plan"))
            closure = set(replan.get("affected_node_keys", ()))
            mutable_keys = closure | (set(backlog.by_key) - set(prior.by_key))
            backlog = apply_replan_boundary(
                prior,
                backlog,
                base_approved_commit=str(replan.get("base_approved_planning_commit", "")),
                selected_root_keys=tuple(replan.get("selected_root_keys", ())),
                affected_node_keys=tuple(replan.get("affected_node_keys", ())),
            )
        mutable_requirements = {
            requirement
            for item in backlog.items
            if item.key in mutable_keys
            for requirement in item.source_requirements
        }
        fabricated_requirements = mutable_requirements - declared_requirements
        if fabricated_requirements:
            raise ValueError(
                "relation draft contains undeclared source requirements: "
                f"{sorted(fabricated_requirements)}"
            )
        covered_requirements = {
            requirement for item in backlog.items for requirement in item.source_requirements
        }
        omitted_requirements = declared_requirements - covered_requirements
        if omitted_requirements:
            raise ValueError(
                f"relation draft omits declared source requirements: {sorted(omitted_requirements)}"
            )
        issues = validate_plan(backlog)
        if issues:
            raise SemanticValidationError(issues)
        return {"backlog": backlog.model_dump(mode="json")}

    async def render_artifacts(state: PlannerState) -> dict[str, Any]:
        backlog = BacklogPlan.model_validate(state["backlog"])
        return {"artifacts": render_artifact_set(cast(dict[str, Any], state), backlog)}

    async def prepare_planning_pr(state: PlannerState) -> dict[str, Any]:
        manifest = artifact_manifest(state["artifacts"])
        return {
            "artifact_manifest": [entry.model_dump(mode="json") for entry in manifest],
            "status": "artifacts_ready",
        }

    builder = StateGraph(PlannerState, context_schema=PlannerRuntimeContext)
    stages = (
        ("normalize_intake", normalize_intake),
        ("classify_scope_and_risk", classify_scope_and_risk),
        ("retrieve_repository_context", retrieve_repository_context),
        ("draft_compact_specification", draft_compact_specification),
        ("identify_consequential_questions", identify_consequential_questions),
        ("human_interrupt_when_required", human_interrupt_when_required),
        ("generate_prd_when_warranted", generate_prd_when_warranted),
        ("generate_architecture", generate_architecture),
        ("derive_requirements_and_decisions", derive_requirements_and_decisions),
        ("decompose_epics_and_stories", decompose_epics_and_stories),
        ("infer_typed_relations", infer_typed_relations),
        ("critique_decomposition", critique_decomposition),
        ("validate_backlog", validate_backlog),
        ("render_artifacts", render_artifacts),
        ("prepare_planning_pr", prepare_planning_pr),
    )
    for name, function in stages:
        builder.add_node(name, function)
    builder.add_edge(START, stages[0][0])
    for (source, _), (target, _) in pairwise(stages):
        builder.add_edge(source, target)
    builder.add_edge(stages[-1][0], END)
    return builder.compile(checkpointer=checkpointer)
