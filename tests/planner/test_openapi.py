from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from planning_platform.planner.api import create_app

CORE_SCHEMAS = {
    "ArtifactBundle",
    "ArtifactContent",
    "ArtifactManifestEntry",
    "IdeaSnapshot",
    "OpenProjectSnapshotInput",
    "PendingInterrupt",
    "PlannerEvent",
    "PlanResponse",
    "RepositoryFile",
    "RepositorySnapshot",
    "ResumePlanRequest",
    "StartPlanRequest",
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
