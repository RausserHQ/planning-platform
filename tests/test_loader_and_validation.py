from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from planning_platform.loader import (
    SchemaValidationError,
    load_artifact,
    load_plan,
    validate_schema,
)
from planning_platform.models import BacklogItem
from planning_platform.validation import validate_plan

FIXTURES = Path(__file__).parents[1] / "evals/fixtures"


def test_all_representative_fixtures_are_valid() -> None:
    fixtures = sorted(FIXTURES.glob("*/backlog.yaml"))
    assert {fixture.parent.name for fixture in fixtures} == {
        "single-repository",
        "cross-repository",
        "architecture-migration",
        "bug-remediation",
        "operational-incident",
        "research-spike",
        "user-facing-feature",
        "data-migration",
        "external-dependency",
        "partial-replan",
    }
    for fixture in fixtures:
        assert validate_plan(load_plan(fixture)) == ()


def test_loaded_artifact_binds_exact_raw_and_git_blob_hashes() -> None:
    artifact = load_artifact(FIXTURES / "single-repository/backlog.yaml")
    raw = (FIXTURES / "single-repository/backlog.yaml").read_bytes()
    assert artifact.raw_bytes == raw
    assert artifact.sha256 == hashlib.sha256(raw).hexdigest()
    assert artifact.blob_sha1 == hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()


def _mutated(**changes: object) -> tuple:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml")
    raw = plan.items[0].model_dump(mode="json")
    raw.update(changes)
    return (plan, BacklogItem.model_validate(raw))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"blocked_by": ("missing-node",)}, "dangling_reference"),
        ({"blocked_by": ("free text blocker",)}, "dangling_reference"),
        ({"source_requirements": (), "maintenance_objectives": ()}, "traceability"),
        ({"source_requirements": ("catch-all",)}, "catch_all_traceability"),
        (
            {
                "acceptance_criteria": (
                    {
                        "criterion": "One outcome and another outcome",
                        "observation": "Run a check command.",
                    },
                )
            },
            "multi_outcome_criterion",
        ),
        (
            {
                "acceptance_criteria": (
                    {
                        "criterion": "Single outcome",
                        "observation": "Manual confirmation is enough.",
                    },
                )
            },
            "unobservable_criterion",
        ),
        (
            {"result_predicate": {"kind": "command", "expression": "success"}},
            "non_objective_predicate",
        ),
        ({"repository": "Acme/unknown"}, "unknown_repository"),
    ],
)
def test_semantic_mutations_are_rejected(changes: dict[str, object], code: str) -> None:
    plan, item = _mutated(**changes)
    altered = plan.model_copy(update={"items": (item,)})
    assert code in {issue.code for issue in validate_plan(altered)}


def test_duplicate_and_cycle_rejection() -> None:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml")
    duplicate = plan.model_copy(update={"items": (plan.items[0], plan.items[0])})
    assert "duplicate_key" in {issue.code for issue in validate_plan(duplicate)}
    cyclic_item = plan.items[0].model_copy(update={"blocked_by": (plan.items[0].key,)})
    cyclic = plan.model_copy(update={"items": (cyclic_item,)})
    assert "dependency_cycle" in {issue.code for issue in validate_plan(cyclic)}


def test_parent_cross_repository_plain_blocker_and_missing_evidence_are_rejected() -> None:
    plan = load_plan(FIXTURES / "cross-repository/backlog.yaml")
    other = plan.items[0].model_copy(update={"key": "other-task", "repository": "Acme/service"})
    child = plan.items[0].model_copy(
        update={
            "parent": other.key,
            "blocked_by": ("plain text blocker",),
            "required_evidence": (),
            "integration_work": False,
        }
    )
    changed = plan.model_copy(update={"items": (other, child)})
    codes = {issue.code for issue in validate_plan(changed)}
    assert {
        "meaningless_parent",
        "cross_repository_parent",
        "plain_text_blocker",
        "missing_evidence",
    } <= codes


def test_schema_rejects_l_size() -> None:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml").model_dump(mode="json")
    plan["items"][0]["estimate"] = "L"
    with pytest.raises(SchemaValidationError):
        validate_schema(plan)


def test_decision_relations_require_decision_items() -> None:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml")
    item = plan.items[0].model_copy(update={"decisions": (plan.items[0].key,)})
    changed = plan.model_copy(update={"items": (item,)})
    assert "invalid_decision_target" in {issue.code for issue in validate_plan(changed)}


def test_typed_model_cannot_bypass_schema_and_relation_projection_limits() -> None:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml")
    invalid = plan.items[0].model_copy(
        update={
            "title": "",
            "blocked_by": ("decision-node",),
            "related_to": ("decision-node",),
        }
    )
    changed = plan.model_copy(update={"items": (invalid,)})
    codes = {issue.code for issue in validate_plan(changed)}
    assert "schema_contract" in codes
    assert "conflicting_relation_semantics" in codes


@given(st.text(min_size=1, max_size=20))
def test_nonempty_unknown_blockers_are_deterministically_reported(blocker: str) -> None:
    plan = load_plan(FIXTURES / "single-repository/backlog.yaml")
    item = plan.items[0].model_copy(update={"blocked_by": (blocker,)})
    issues = validate_plan(plan.model_copy(update={"items": (item,)}))
    assert any(
        issue.code in {"dangling_reference", "self_reference", "dependency_cycle"}
        for issue in issues
    )
