"""Verify a Windmill v2 HTTP trigger before any lifecycle effect."""

from __future__ import annotations

import os
from typing import Any, Literal

from planning_platform.lifecycle.ingress import (
    github_envelope,
    openproject_envelope,
)


def _secret(name: str) -> bytes:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value.encode("utf-8")


def main(
    source: Literal["github", "openproject"],
    event: dict[str, Any],
) -> dict[str, Any]:
    if source == "github":
        envelope = github_envelope(
            event,
            secret=_secret("GITHUB_WEBHOOK_SECRET"),
        )
    elif source == "openproject":
        envelope = openproject_envelope(
            event,
            secret=_secret("OPENPROJECT_WEBHOOK_SECRET"),
        )
    else:
        raise ValueError("unsupported webhook source")
    return envelope.model_dump(mode="json")
