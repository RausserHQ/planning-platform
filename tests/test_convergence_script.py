from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from planning_platform.lifecycle.ingress import convergence_check_envelope
from planning_platform.lifecycle.service import LifecycleOutcome


def _script() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "windmill/f/planning/convergence_check.py"
    )
    spec = importlib.util.spec_from_file_location("convergence_check_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event():
    return convergence_check_envelope(
        plan_id="single-repository",
        plan_version=1,
        delivery_id="wm-root-job-1176",
        occurred_at=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
    )


def test_convergence_script_marks_only_zero_operations_successful() -> None:
    script = _script()
    zero = LifecycleOutcome(
        action="convergence_check",
        outcome="zero_operations",
        plan_id="single-repository",
        plan_version=1,
    )
    drift = LifecycleOutcome(
        action="convergence_check",
        outcome="drift_operations:2",
        plan_id="single-repository",
        plan_version=1,
    )

    assert script._windmill_result(_event(), zero)["outcome"] == "zero_operations"
    failed = script._windmill_result(_event(), drift)
    assert failed["wm_failure"] == "drift_operations:2"
    assert failed["result"]["outcome"] == "drift_operations:2"
    assert failed["event"]["source_delivery_id"] == "convergence:wm-root-job-1176"


def test_convergence_script_fails_closed_when_a_completed_result_is_unavailable() -> None:
    result = _script()._windmill_result(_event(), "deduplicated:completed")

    assert result["wm_failure"] == "convergence_result_unavailable"
    assert result["result"] == "deduplicated:completed"


def test_convergence_script_rebinds_the_envelope_to_the_current_root_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    monkeypatch.setenv("WM_ROOT_FLOW_JOB_ID", "wm-root-job-1176")
    assert script._trusted_envelope(_event().model_dump(mode="json")) == _event()

    forged = convergence_check_envelope(
        plan_id="single-repository",
        plan_version=1,
        delivery_id="caller-controlled",
        occurred_at=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="root job"):
        script._trusted_envelope(forged.model_dump(mode="json"))


def test_convergence_failure_handler_preserves_red_status() -> None:
    path = Path(__file__).parents[1] / "windmill/f/planning/dead_letter.py"
    spec = importlib.util.spec_from_file_location("dead_letter_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module._handler_result(
        {"state": "completed", "message": "delivery already completed"},
        preserve_failure=True,
    )

    assert result["wm_failure"] == "convergence_check_failed"
    assert result["result"]["state"] == "completed"
