from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import APIStatusError
from pydantic import SecretStr

from planning_platform.planner import model as model_module
from planning_platform.planner.api import PlannerSettings
from planning_platform.planner.models import (
    CompactSpecification,
    ConsequentialQuestions,
    DecompositionCritique,
    DecompositionDraft,
    DocumentSection,
    RelationDraft,
    RequirementsDraft,
    ScopeClassification,
)

STAGE_SCHEMAS = (
    ScopeClassification,
    CompactSpecification,
    ConsequentialQuestions,
    DocumentSection,
    RequirementsDraft,
    DecompositionDraft,
    RelationDraft,
    DecompositionCritique,
)


def test_production_model_uses_private_responses_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(model_module, "ChatOpenAI", FakeChatOpenAI)

    model_module.ChatOpenAIPlanningModel(
        "gpt-5.6-sol",
        reasoning_effort="medium",
    )

    assert captured == {
        "model": "gpt-5.6-sol",
        "use_responses_api": True,
        "output_version": "responses/v1",
        "reasoning": {"effort": "medium"},
        "store": False,
    }


def test_planner_settings_default_and_validate_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANNER_DATABASE_URL", "postgresql://planner")
    monkeypatch.setenv("PLANNER_OPENAI_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("PLANNER_INTERNAL_TOKEN", "internal")
    monkeypatch.delenv("PLANNER_OPENAI_REASONING_EFFORT", raising=False)

    settings = PlannerSettings.from_environment()
    assert settings.reasoning_effort == "medium"

    monkeypatch.setenv("PLANNER_OPENAI_REASONING_EFFORT", "ultra")
    with pytest.raises(
        RuntimeError,
        match="planner reasoning effort must be one of",
    ):
        PlannerSettings.from_environment()

    monkeypatch.setenv("PLANNER_OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("PLANNER_OPENAI_MODEL", "gpt-4o")
    with pytest.raises(RuntimeError, match=r"planner model must be gpt-5\.6-sol"):
        PlannerSettings.from_environment()


@pytest.mark.asyncio
async def test_every_stage_emits_an_openai_strict_compatible_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            418,
            json={
                "error": {
                    "message": "captured",
                    "type": "test",
                    "param": None,
                    "code": None,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:

        def client(**kwargs: Any) -> ChatOpenAI:
            return ChatOpenAI(
                **kwargs,
                api_key=SecretStr("test"),
                http_async_client=http,
                max_retries=0,
            )

        monkeypatch.setattr(model_module, "ChatOpenAI", client)
        model = model_module.ChatOpenAIPlanningModel("gpt-5.6-sol")

        for schema in STAGE_SCHEMAS:
            with pytest.raises(APIStatusError, match="captured"):
                await model.generate("schema_contract", schema, {})

    assert len(requests) == len(STAGE_SCHEMAS)
    schema_names = tuple(schema.__name__ for schema in STAGE_SCHEMAS)
    for request, schema_name in zip(requests, schema_names, strict=True):
        assert request["model"] == "gpt-5.6-sol"
        assert request["reasoning"] == {"effort": "medium"}
        assert request["store"] is False
        output = request["text"]["format"]
        assert output["type"] == "json_schema"
        assert output["name"] == schema_name
        assert output["strict"] is True
        _assert_openai_strict_schema(output["schema"])


def _assert_openai_strict_schema(value: Any) -> None:
    if isinstance(value, dict):
        assert "default" not in value
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert value["additionalProperties"] is False
            assert value["required"] == list(properties)
        for item in value.values():
            _assert_openai_strict_schema(item)
    elif isinstance(value, list):
        for item in value:
            _assert_openai_strict_schema(item)
