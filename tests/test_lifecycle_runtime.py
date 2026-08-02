from __future__ import annotations

import pytest

from planning_platform.lifecycle.runtime import _bounded_integer, _positive_integer


@pytest.mark.parametrize(("value", "expected"), (("1", 1), ("3600", 3600)))
def test_positive_integer_environment_value(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    monkeypatch.setenv("PLANNING_THREAD_STALE_SECONDS", value)
    assert _positive_integer("PLANNING_THREAD_STALE_SECONDS") == expected


@pytest.mark.parametrize("value", ("0", "-1", "not-an-integer"))
def test_positive_integer_rejects_invalid_environment_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PLANNING_THREAD_STALE_SECONDS", value)
    with pytest.raises(RuntimeError, match="must be"):
        _positive_integer("PLANNING_THREAD_STALE_SECONDS")


@pytest.mark.parametrize(("value", "expected"), (("31", 31), ("600", 600), ("900", 900)))
def test_bounded_integer_environment_value(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    monkeypatch.setenv("PLANNER_HTTP_TIMEOUT_SECONDS", value)
    assert (
        _bounded_integer("PLANNER_HTTP_TIMEOUT_SECONDS", minimum=31, maximum=900)
        == expected
    )


def test_bounded_integer_uses_safe_default_only_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLANNER_HTTP_TIMEOUT_SECONDS", raising=False)
    assert (
        _bounded_integer(
            "PLANNER_HTTP_TIMEOUT_SECONDS",
            minimum=31,
            maximum=900,
            default=600,
        )
        == 600
    )
    monkeypatch.setenv("PLANNER_HTTP_TIMEOUT_SECONDS", "")
    with pytest.raises(RuntimeError, match="is required"):
        _bounded_integer(
            "PLANNER_HTTP_TIMEOUT_SECONDS",
            minimum=31,
            maximum=900,
            default=600,
        )


@pytest.mark.parametrize("value", ("30", "901", "not-an-integer"))
def test_bounded_integer_rejects_invalid_environment_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PLANNER_HTTP_TIMEOUT_SECONDS", value)
    with pytest.raises(RuntimeError, match="must be between 31 and 900"):
        _bounded_integer("PLANNER_HTTP_TIMEOUT_SECONDS", minimum=31, maximum=900)
