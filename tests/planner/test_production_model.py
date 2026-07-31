from __future__ import annotations

from typing import Any

import pytest

from planning_platform.planner import model as model_module
from planning_platform.planner.api import PlannerSettings


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
        match="PLANNER_OPENAI_REASONING_EFFORT must be one of",
    ):
        PlannerSettings.from_environment()
