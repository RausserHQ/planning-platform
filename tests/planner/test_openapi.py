from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from planning_platform.planner.api import create_app

CORE_SCHEMAS = {
    "AbandonTerminalResumeRequest",
    "AcceptanceCriterion",
    "AgentEligibility",
    "ArtifactBundle",
    "ArtifactContent",
    "ArtifactManifestEntry",
    "BacklogItem",
    "BacklogPlan",
    "IdeaSnapshot",
    "OpenProjectSnapshotInput",
    "PendingInterrupt",
    "Plan",
    "PlannerEvent",
    "PlanResponse",
    "ReplanContext",
    "ReplanNodeBinding",
    "ReplanScope",
    "Repository",
    "RepositoryFile",
    "RepositorySnapshot",
    "RequiredEvidence",
    "ResultPredicate",
    "ResumePlanRequest",
    "StartPlanRequest",
    "SnapshotReference",
    "SourceIdea",
}


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items() if key != "title"}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _operations(specification: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (method, path, operation["operationId"])
        for path, methods in specification["paths"].items()
        for method, operation in methods.items()
    }


def test_static_openapi_matches_runtime_operations_security_and_core_schemas() -> None:
    path = Path(__file__).parents[2] / "docs/api/planner.openapi.yaml"
    static = yaml.safe_load(path.read_text())
    runtime = create_app().openapi()

    assert _operations(static) == _operations(runtime)
    assert _normalize(static["components"]["securitySchemes"]) == _normalize(
        runtime["components"]["securitySchemes"]
    )
    for schema in CORE_SCHEMAS:
        assert _normalize(static["components"]["schemas"][schema]) == _normalize(
            runtime["components"]["schemas"][schema]
        )
    for route, methods in runtime["paths"].items():
        for operation in methods.values():
            if route.startswith("/v1/"):
                assert operation["security"] == [{"InternalToken": []}]
            else:
                assert "security" not in operation


def test_replan_openapi_enforces_the_artifact_identity_bounds() -> None:
    path = Path(__file__).parents[2] / "docs/api/planner.openapi.yaml"
    schemas = yaml.safe_load(path.read_text())["components"]["schemas"]

    for schema_name in ("ReplanContext", "ReplanScope"):
        schema = schemas[schema_name]
        for field in ("selected_root_keys", "affected_node_keys"):
            values = schema["properties"][field]
            assert values["minItems"] == 1
            assert values["uniqueItems"] is True
            assert values["items"] == {
                "type": "string",
                "minLength": 3,
                "maxLength": 96,
                "pattern": "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
            }

    scope = schemas["ReplanScope"]["properties"]
    assert scope["base_plan_version"]["minimum"] == 1
    assert scope["retained_node_bindings"]["uniqueItems"] is True
    binding = schemas["ReplanNodeBinding"]["properties"]
    assert binding["node_key"] == {
        "type": "string",
        "minLength": 3,
        "maxLength": 96,
        "pattern": "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    }
    assert binding["plan_version"]["minimum"] == 1
    assert binding["planning_commit"]["pattern"] == "^[0-9a-f]{40}$"
