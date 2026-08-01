from __future__ import annotations

import pytest

from planning_platform.lifecycle.runtime import _positive_integer


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
