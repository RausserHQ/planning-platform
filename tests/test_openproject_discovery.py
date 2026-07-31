from __future__ import annotations

import json

import httpx
import pytest

from planning_platform.openproject_discovery import (
    OpenProjectDiscoveryError,
    discover_openproject_config,
)

TYPE_NAMES = (
    "Idea",
    "Initiative",
    "Epic",
    "Story",
    "Task",
    "Decision",
    "Investigation",
    "Bug",
)
STATUS_NAMES = (
    "Draft",
    "Planning",
    "Needs Input",
    "Proposed",
    "Ready",
    "In Progress",
    "Blocked",
    "Review",
    "Done",
    "Superseded",
    "Rejected",
)
FIELD_NAMES = (
    "Plan ID",
    "Node key",
    "Plan version",
    "Managed hash",
    "Repository",
    "Risk",
    "Agent eligible",
    "Source requirements",
    "Planning commit",
    "Evidence state",
)


def _collection(values: list[dict[str, object]]) -> dict[str, object]:
    return {
        "count": len(values),
        "total": len(values),
        "_embedded": {"elements": values},
    }


def _client(*, duplicate_type: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/projects":
            filters = json.loads(request.url.params["filters"])
            assert filters[0]["name_and_identifier"]["values"] == ["planning-platform"]
            return httpx.Response(
                200,
                json=_collection(
                    [{"id": 42, "identifier": "planning-platform", "name": "Planning Platform"}]
                ),
            )
        if request.url.path == "/api/v3/projects/42/types":
            values = [{"id": index + 10, "name": name} for index, name in enumerate(TYPE_NAMES)]
            if duplicate_type:
                values.append({"id": 99, "name": "Idea"})
            return httpx.Response(200, json=_collection(values))
        if request.url.path == "/api/v3/statuses":
            return httpx.Response(
                200,
                json=_collection(
                    [{"id": index + 20, "name": name} for index, name in enumerate(STATUS_NAMES)]
                ),
            )
        if request.url.path == "/api/v3/priorities":
            return httpx.Response(
                200,
                json=_collection(
                    [
                        {"id": 50, "name": "Low", "isActive": True},
                        {"id": 51, "name": "Normal", "isActive": True},
                        {"id": 52, "name": "High", "isActive": True},
                        {"id": 53, "name": "Immediate", "isActive": True},
                    ]
                ),
            )
        if request.url.path == "/api/v3/work_packages/schemas/42-10":
            return httpx.Response(
                200,
                json={
                    "_type": "Schema",
                    **{
                        f"customField{index + 40}": {"name": name}
                        for index, name in enumerate(FIELD_NAMES)
                    },
                },
            )
        return httpx.Response(404)

    return httpx.Client(
        base_url="https://openproject.test",
        transport=httpx.MockTransport(handler),
    )


def test_discovers_exact_bootstrapped_openproject_contract() -> None:
    with _client() as client:
        config = discover_openproject_config(
            base_url="https://openproject.test",
            project_identifier="planning-platform",
            token="secret",
            client=client,
        )
    assert config.project_id == 42
    assert config.type_ids["Idea"] == 10
    assert config.status_ids["Done"] == 28
    assert config.custom_field_ids["evidence_state"] == 49
    assert config.priority_ids == {
        "low": 50,
        "medium": 51,
        "high": 52,
        "critical": 53,
    }


def test_discovery_rejects_ambiguous_semantic_name() -> None:
    with (
        _client(duplicate_type=True) as client,
        pytest.raises(OpenProjectDiscoveryError, match="duplicated"),
    ):
        discover_openproject_config(
            base_url="https://openproject.test",
            project_identifier="planning-platform",
            token="secret",
            client=client,
        )
